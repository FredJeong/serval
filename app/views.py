#-*-coding: utf-8 -*-
from app import config, app, api, db, models, lm, forms
from flask import render_template, Response, redirect, url_for, request, abort, session
from flask.ext.restful import Resource, reqparse, fields, marshal_with, inputs

from mongoengine.queryset import DoesNotExist
from mongoengine import ValidationError

from flask.ext.login import login_user, logout_user, current_user, \
    login_required, AnonymousUserMixin

from flask_oauth import OAuth

oauth = OAuth()

facebook = oauth.remote_app('facebook',
    base_url='https://graph.facebook.com/',
    request_token_url=None,
    access_token_url='/oauth/access_token',
    authorize_url='https://www.facebook.com/dialog/oauth',
    consumer_key=config.SOCIAL_FACEBOOK['consumer_key'],
    consumer_secret=config.SOCIAL_FACEBOOK['consumer_secret'],
    request_token_params={'scope': ('email, user_friends, ')}
)

lm.login_view = 'facebook_login'

@lm.user_loader
def load_user(id):
    return models.User.objects(facebook_id=id).first()

@app.errorhandler(404)
def not_found(e):
    return "HTTP 404 NOT FOUND"

@app.errorhandler(403)
def not_found(e):
    return "HTTP 403 FORBIDDEN"

@app.errorhandler(401)
def not_found(e):
    return redirect('/')

@facebook.tokengetter
def get_facebook_token():
    return session.get('facebook_token')

def pop_login_session():
    session.pop('logged_in', None)
    session.pop('facebook_token', None)
    session.pop('user_id', None)
    session.pop('user_name', None)

@app.route('/facebook_login')
def facebook_login():
    return facebook.authorize(callback=url_for('facebook_authorized',
        next=request.args.get('next'), _external=True))

@app.route('/facebook_authorized')
@facebook.authorized_handler
def facebook_authorized(resp):
    next_url = request.args.get('next') or url_for('index')
    if resp is None or 'access_token' not in resp:
        return redirect(next_url)
    session['logged_in'] = True
    session['facebook_token'] = (resp['access_token'], '')

    data = facebook.get('/me').data
    print(data)
    if 'id' in data and 'name' in data:
        session['user_id'] = data['id']
        session['user_name'] = data['name']
    user = models.User.objects(facebook_id=session['user_id']).first()
    if user is None:
        user = models.User(facebook_id=session['user_id'])
        user.save()
    if user.name != session['user_name']:
        user.name = session['user_name']
        user.save()
    login_user(user)

    friends = []
    friends_query = facebook.get('/me/friends?limit=500')
    friends = friends_query.data
    user.update_friends(friends)

    return redirect(next_url)

@app.route('/logout')
def logout():
    pop_login_session()
    return redirect(url_for('index'))

@app.route('/')
def index():
    if 'logged_in' in session and session['logged_in']:
        if 'user_id' not in session or session['user_id']:
            data = facebook.get('/me').data
            if 'id' in data and 'name' in data:
                session['user_id'] = data['id']
                session['user_name'] = data['name']
            else:
                print("data does not contain id/name")
        user = models.User.objects(facebook_id=session['user_id']).first()
        petitions = models.Petition.objects(author=user)
        friend_petitions = []
        for friend in user.friends:
            f_petitions = models.Petition.objects(author=friend)
            friend_petitions.append((friend, f_petitions))
    else:
        return render_template('login.html')

    return render_template(
        'index.html',
        session=session,
        petitions=petitions, friend_petitions=friend_petitions)

@app.route('/petition/create')
@login_required
def add_petition():
    form = forms.PetitionForm()
    return render_template(
        'add_petition.html',
        session=session,
        form=form)

@app.route('/petition/<string:uid>')
def view_petition(uid):
    try:
        petition = models.Petition.objects(id=uid).first()
    except ValidationError:
        print("Petition ID invalid")
        abort(404)
    if petition is None:
        abort(404)

    return render_template(
        'petition.html',
        session=session,
        petition=petition)

@app.route('/user/<string:uid>')
@login_required
def view_user(uid):
    if uid == str(current_user.id) or \
        uid == str(current_user.facebook_id):
        return redirect('/')
    try:
        user = models.User.objects(id=uid).first()
    except ValidationError:
        user = models.User.objects(facebook_id=uid).first()
    if user is None:
        abort(404)

    petitions = models.Petition.objects(author=user)

    return render_template(
        'user.html',
        session=session,
        user=user,
        petitions=petitions)


class Petition(Resource):
    @login_required
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument('title', type=unicode)
        parser.add_argument('content', type=unicode)
        parser.add_argument('summary', type=unicode)
        parser.add_argument('cover_link', type=unicode)
        parser.add_argument('video_link', type=unicode)
        parser.add_argument('items[]', dest='items', type=unicode, action='append')

        args = parser.parse_args()
        print(args['items'])
        items = map(lambda x: x.split('|', 2), args['items'])
        items = map(lambda x:
            models.Item(target_fund=int(x[0]),
                recommended_fund=int(x[1]), description=x[2]).save(),
            items)
        petition = models.Petition(
            title=args['title'], content=args['content'], summary=args['summary'],
            cover_link=args['cover_link'], video_link=args['video_link'],
            items=items)
        petition.save()
        user = models.User.objects(facebook_id=session['user_id']).first()
        petition.author = user
        petition.save()


        return petition.__dict__(), 201

    def get(self, uid):
        obj = models.Petition.objects(id=uid).first()
        if obj is None:
            return None, 404
        return obj.__dict__(), 200

class Donation(Resource):
    @login_required
    def put(self, uid):
        parser = reqparse.RequestParser()
        parser.add_argument('balance', type=int)
        parser.add_argument('message', type=unicode)
        args = parser.parse_args()
        item = models.Item.objects(id=uid).first()
        if item is None:
            abort(404)

        donation = models.Donation(
            balance=args['balance'], message=args['message'])
        user = models.User.objects(facebook_id=session['user_id']).first()
        donation.user = user
        item.pending_fund += args['balance']
        item.donations.append(donation)
        item.save()

        return item.__dict__(), 200

class DonationConfirm(Resource):
    @login_required
    def put(self, uid):
        parser = reqparse.RequestParser()
        parser.add_argument('cancel', type=inputs.boolean, default=False)
        parser.add_argument('balance', type=int)
        parser.add_argument('user_id', type=str)
        parser.add_argument('index', type=int)
        args = parser.parse_args()

        item = models.Item.objects(id=uid).first()
        if item is None:
            print("No item for %s" % uid)
            abort(404)

        donation = item.donations[args['index']]
        petition = models.Petition.objects(items=item).first()

        if petition is None:
            print("No petition found")
            abort(404)

        if str(petition.author.facebook_id) != session['user_id']:
            print("User does not match")
            abort(403)

        if str(donation.user.id) == args['user_id'] and donation.balance == args['balance']:
            donation.pending = args['cancel']
            item.update_fund()
        item.save()

        return item.__dict__(), 200

    @login_required
    def delete(self, uid):
        parser = reqparse.RequestParser()
        parser.add_argument('balance', type=int)
        parser.add_argument('user_id', type=str)
        parser.add_argument('index', type=int)
        args = parser.parse_args()

        item = models.Item.objects(id=uid).first()
        if item is None:
            print("No item for %s" % uid)
            abort(404)

        donation = item.donations[args['index']]

        if str(donation.user.facebook_id) == session['user_id'] and donation.balance == args['balance']:
            item.donations.remove(donation)
            item.update_fund()
        item.save()

        return item.__dict__(), 200

api.add_resource(Petition, '/api/petition', '/api/petition/<string:uid>')
api.add_resource(Donation, '/api/item/<string:uid>/fund')
api.add_resource(DonationConfirm, '/api/item/<string:uid>/donation/confirm')