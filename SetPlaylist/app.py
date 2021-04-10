import os

import json
import requests
import tekore
import urllib.parse
from dotenv import load_dotenv
from flask import Flask, g, redirect, render_template, request, session, jsonify, abort
from flask_debugtoolbar import DebugToolbarExtension
from forms import (
    ForgotPassAnswer,
    ForgotPassUsername,
    LoginForm,
    PasswordReset,
    RegisterForm,
    UserEditForm,
)
from models import (
    Band,
    Favorite,
    Playlist,
    Playlist_Song,
    Song,
    User,
    User_Playlist,
    connect_db,
    db,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound

load_dotenv()


app = Flask(__name__)


app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URI", "postgres:///setplaylist-test"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ECHO"] = False
app.config["DEBUG_TB_INTERCEPT_REDIRECTS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "secret!")
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True

CURR_USER_KEY = os.environ.get("CURR_USER_KEY")
APP_TOKEN = tekore.request_client_token(
    os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
)

conf = tekore.config_from_environment(return_refresh=True)
cred = tekore.Credentials(*conf)
spotify = tekore.Spotify(APP_TOKEN)

auths = {}
users = {}
scope = (
    tekore.scope.playlist_modify_private
    + tekore.scope.playlist_read_private
    + tekore.scope.user_read_private
    + tekore.scope.playlist_read_collaborative
)

toolbar = DebugToolbarExtension(app)

connect_db(app)

##################
# Global Methods ######################
##################


def session_login(user):
    """
    Login user to Flask session
    """
    session[CURR_USER_KEY] = user.id

    return None


def session_logout(user):
    """
    Logout user from Flask session
    """
    if CURR_USER_KEY in session:
        del session[CURR_USER_KEY]
    uid = session.pop("user", None)
    if uid is not None:
        users.pop(uid, None)

    return None


@app.context_processor
def utility_processor():
    def format_name(name):
        """
        Returns a url-safe string
        """
        fixed = name.replace("/", "--sls--")
        fixed = urllib.parse.quote(fixed, safe="")
        return fixed

    def format_setlist_display(set):
        venue_name = set["venue"]["name"]
        event_date = set["eventDate"]
        venue_loc = (
            set["venue"]["city"]["name"]
            + ", "
            + set["venue"]["city"]["stateCode"]
            + ", "
            + set["venue"]["city"]["country"]["code"]
        )
        if venue_name == "":
            venue_name = "Venue Unknown"
        return f"{venue_name} - {event_date} - {venue_loc}"

    return dict(format_name=format_name, format_setlist_display=format_setlist_display)


def unformat_name(name):
    """
    Returns the decoded url component
    """
    return name.replace("--sls--", "/")


##############################
# User Login/Logout/Register ##########
##############################


@app.before_request
def add_to_g():
    """
    If user logged in, add to Flask global
    """
    if CURR_USER_KEY in session:
        g.user = User.query.get(session[CURR_USER_KEY])
    else:
        g.user = None


@app.route("/register", methods=["GET", "POST"])
def register():
    """
    GET ROUTE:
    - Display register form
    --------------------
    POST ROUTE:
    Handle user registration
    - Create new user and add to DB
    - Redirect to Landing Page
    - If form not valid, present form
    - If username in user
        - Add username error message and re-present form
    """
    if not g.user:
        form = RegisterForm()

        if form.validate_on_submit():
            # POST ROUTE FOR REGISTRATION FORM
            try:
                user = User.register(
                    username=form.username.data,
                    password=form.password.data,
                    email=form.email.data,
                    secret_question=form.secret_question.data,
                    secret_answer=form.secret_answer.data,
                )
                db.session.commit()
            except IntegrityError:
                form.username.errors.append("Username not available")
                return render_template(
                    "auth.html", form=form, title="Register", q_display=""
                )

            session_login(user)

            # Spotify authorization flow
            auth = tekore.UserAuth(cred, scope)
            auths[auth.state] = auth

            return redirect(auth.url, 303)

        return render_template("auth.html", form=form, title="Register", q_display="")
    else:
        abort(403)


@app.route("/callback")
def spotify_callback():
    """
    GET ROUTE:
    - Spotify callback route
    """
    if request.args.get("code"):
        user = User.query.get_or_404(session[CURR_USER_KEY])

        code = request.args.get("code")
        state = request.args.get("state", None)
        auth = auths.pop(state, None)

        if auth is None:
            abort(500)

        token = auth.request_token(code, state)
        user.spotify_user_token = token.refresh_token
        spotify.token = user.spotify_user_token
        db.session.add(user)
        db.session.commit()

        session["user"] = state
        users[state] = token

        return redirect("/user/home")

    abort(403)


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    GET ROUTE:
    - Display login form
    --------------------
    POST ROUTE:
    - Handle user login
    """
    if g.user:
        return redirect("/user/home")

    form = LoginForm()

    if form.validate_on_submit():
        user = User.authenticate(form.username.data, form.password.data)

        if user:
            session_login(user)
            token = cred.refresh(user.spotify_user_token)
            user.spotify_user_token = token
            return redirect("/user/home")
        form.username.errors.append("Invalid username/password")

    return render_template("auth.html", form=form, title="Login", q_display="")


@app.route("/logout", methods=["POST"])
def logout():
    """
    POST ROUTE:
    - Handle logout of user
    """
    user = session[CURR_USER_KEY]
    session_logout(user)
    return redirect("/")


#########################
# Reset Password Routes ###############
#########################


@app.route("/forgot", methods=["GET", "POST"])
def forgot_password_check_username():
    """
    GET ROUTE:
    - Display form for username entry for forgotten password
    --------------------
    POST ROUTE:
    - Checks that username is in database
    - If username is in database
        - Redirects to next page
    - If username not in database
        - Display error, redisplay form
    """
    form = ForgotPassUsername()

    if g.user:
        abort(403)

    if form.validate_on_submit():
        try:
            user = User.query.filter_by(username=form.username.data).one()
        except NoResultFound or MultipleResultsFound:
            form.username.errors.append("Username not found")
            return render_template(
                "auth.html", title="Forgot Password", form=form, q_display=""
            )

        session["password_reset"] = True
        return redirect(f"/forgot/{user.id}")

    return render_template(
        "auth.html", title="Forgot Password", form=form, q_display=""
    )


@app.route("/forgot/<int:user_id>", methods=["GET", "POST"])
def forgot_password_check_secret_question(user_id):
    """
    GET ROUTE:
    - Display form for secret question/answer
    --------------------
    POST ROUTE:
    - Check the secret answer
    - Redirect to '/forgot/<user_id>/new'
    """
    if g.user:
        abort(403)

    form = ForgotPassAnswer()

    if not session.get("password_reset"):
        return forbidden()

    user = User.query.get_or_404(user_id)

    if form.validate_on_submit():
        if User.authenticate_secret_answer(user.username, form.secret_answer.data):
            return redirect("/forgot/<user_id>/new")
        else:
            form.secret_answer.errors.append("Invalid secret answer")

    form.secret_question.data = user.secret_question

    return render_template(
        "/auth.html", form=form, title="Forgot Password", q_display="q_display"
    )


@app.route("/forgot/<int:user_id>/new", methods=["GET", "POST"])
def forgot_password_new_password(user_id):
    """
    GET ROUTE:
    - Display form for password reset
    --------------------
    POST ROUTE:
    - Check that password fields match
    - Save change to database
    - Redirect to login page
    """
    if g.user:
        abort(403)

    form = PasswordReset()

    if not session.get("password_reset"):
        return forbidden()

    user = User.query.get_or_404(user_id)

    if form.validate_on_submit():
        if form.new_password.data == form.retype_password.data:
            hashed_pwd = User.hash_password(form.new_password.data)
            user.password = hashed_pwd

            db.add(user)
            db.commit()

            session.pop("password_reset")

            return redirect("/login")
        else:
            form.new_password.errors.append("Passwords must match")
            return render_template(
                "auth.html", title="Reset Password", form=form, q_display=""
            )

    return render_template("auth.html", form=form, title="Reset Password", q_display="")


################
# Landing Page ########################
################


@app.route("/")
def landing():
    """
    GET ROUTE:
    - If user logged in, redirect to '/user/home'
    - If logged out, return logged out landing page
    """
    if g.user:
        return redirect("/user/home")
    else:
        return render_template("landing.html")


###############
# User Routes #########################
###############


@app.route("/user/home")
def homepage():
    """
    GET ROUTE:
    - If user logged out, redirect to '/'
    - If logged in, return logged in homepage
    """
    if not g.user:
        return redirect("/")
    else:
        recent_playlists = Playlist.query.order_by(Playlist.id.desc()).limit(10)

    return render_template("/user/home.html", recent_playlists=recent_playlists)


@app.route("/user/<int:user_id>/edit", methods=["GET", "POST"])
def edit_user(user_id):
    """
    GET ROUTE:
    - Displays User Edit Form
    --------------------
    POST ROUTE:
    - Checks password
    - If passes
        - Updates any data changed
        - Commits changes to database
        - Redirects to User Homepage
    """
    form = UserEditForm()

    if not g.user:
        abort(403)

    if form.validate_on_submit():
        if not form.secret_question and not form.secret_question:
            form.secret_question.errors.append(
                "Must change both secret question and answer together"
            )
            form.secret_answer.errors.append(
                "Must change both secret question and answer together"
            )
            return redirect(f"/user/{user_id}/edit")

        current_password = form.current_password.data
        user = User.query.get_or_404(user_id)

        if User.authenticate(user.username, current_password):
            user.username = form.username.data or user.username
            user.email = form.email.data or user.email
            user.secret_question = form.secret_question.data or user.secret_question
            user.secret_answer = form.secret_answer.data or user.secret_answer

            new_password = form.new_password.data or None
            retype_password = form.retype_password.data or None
            if (
                new_password is not None
                and retype_password is not None
                and new_password == retype_password
            ):
                user.password = User.hash_password(new_password)

            try:
                db.add(user)
                db.commit()
            except IntegrityError:
                form.username.errors.append("Username unavailable")
                return redirect(f"/user/{user_id}/edit")

            return redirect("/user/home")
        else:
            form.password.errors.append("Incorrect Password")

    return render_template(
        "/user/edit.html", form=form, title="Edit User", q_display=""
    )


###############
# Band Routes #########################
###############


@app.route("/band/<band_id>")
def show_band_details(band_id):
    """
    Todo - Shows band details
    """

    band = Band.query.filter_by(spotify_artist_id=band_id).first()

    if band is None:

        ##################################
        # Spotify
        #   - Get band
        #   - Set band_image variable
        res = spotify.artist(band_id)
        json_res = res.json()
        sp_band = json.loads(json_res)

        band_name = sp_band["name"]

        try:
            band_image = sp_band["images"][0]["url"]
        except IndexError:
            band_image = "/static/img/rocco-dipoppa-_uDj_lyPVpA-unsplash.jpg"
        # Spotify End
        #################################

        ###############################
        # Setlist.fm search band
        url = os.environ.get("SETLIST_FM_BASE_URL") + "/search/artists"
        res = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "x-api-key": os.environ.get("SETLIST_FM_API_KEY"),
            },
            params=[("artistName", band_name)],
        ).json()

        fm_band = {}

        # FIXME: This is checking for only EXACT matches, need to check for case-insensitive
        try:
            for band in res["artist"]:
                if band["name"] == band_name:
                    fm_band = band
        except KeyError:
            fm_band = None
        # Setlist.fm search band end
        ##############################

        ##############################
        # Setlist.fm setlists search
        if fm_band is not None:
            url = (
                os.environ.get("SETLIST_FM_BASE_URL")
                + f"/artist/{fm_band['mbid']}/setlists"
            )

            res = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "x-api-key": os.environ.get("SETLIST_FM_API_KEY"),
                },
            ).json()

            setlists = res["setlist"]
        else:
            setlists = None
        # Setlist.fm setlists search end
        ##############################

        ################################
        # Bandsintown Upcoming Events Search
        bit_search_name = Band.bit_prep_band_name(band_name)

        url = (
            os.environ.get("BANDSINTOWN_BASE_URL")
            + "/artists/"
            + bit_search_name
            + "/events/"
        )
        upcoming_shows = requests.get(
            url,
            headers={"accept": "application/json"},
            params=[("app_id", os.environ.get("BIT_APP_ID"))],
        ).json()

        if type(upcoming_shows) != list:
            upcoming_shows = None

        # Bandsintown Upcoming Events Search End
        ###################################

    return render_template(
        "/band/band-detail.html",
        band=sp_band,
        upcoming_shows=upcoming_shows,
        band_image=band_image,
        setlists=setlists,
    )

    # If logged in - band, setlists, upcoming_shows, band_image(url)
    # If not - band, band_image(url)


######################
# Band Search Routes ##################
######################


@app.route("/band/search")
def search_results():
    """
    GET ROUTE:
    - Display search results
    OR
    - Display search form
    """
    if request.args.get("search"):
        search = request.args.get("search")
        res = spotify.search("artist: " + search, types=["artist"])
        json_res = res[0].json()
        band_results = json.loads(json_res)

        return render_template(
            "/band/search.html", search=search, band_results=band_results["items"]
        )
    else:
        return render_template("band/search.html")


###################
# Playlist Routes #####################
###################


# TODO:
@app.route("/playlist/<band_id>/<setlist_id>")
def show_setlist(band_id, setlist_id):
    """
    GET ROUTE: - Shows the setlist data that was selected
    """
    res = spotify.artist(band_id)
    json_res = res.json()
    sp_band = json.loads(json_res)

    url = os.environ.get("SETLIST_FM_BASE_URL") + f"/setlist/{setlist_id}"
    res = requests.get(
        url,
        headers={
            "Accept": "application/json",
            "x-api-key": os.environ.get("SETLIST_FM_API_KEY"),
        },
    ).json()

    details = Playlist.details(res)

    setlist = res["sets"]["set"]

    playlist = []

    for set in setlist:
        for song in set["song"]:
            playlist.append(song["name"])

    return render_template(
        "/playlist/playlist.html",
        saved=False,
        band=sp_band,
        details=details,
        playlist=playlist,
    )


@app.route("/playlist/<int:playlist_id>")
def show_created_playlist(playlist_id):
    """
    Todo - Shows the setlist that was created
    """
    # playlist (Playlist object saved to db), saved, not_included (songs not in the playlist that were on the setlist)
    return render_template(
        "/playlist/playlist.html",
        saved=True,
        # playlist=playlist,
        # not_included=not_included,
    )


@app.route("/playlist/<int:band_id>/hype")
def show_hype_setlist(band_id):
    """
    Todo - Shows the setlist created from band's top songs
    """
    saved = True
    # playlist (Playlist object saved to db, created from band's top songs from spotify), saved
    return render_template("/playlist/playlist.html", saved=saved)


@app.route("/playlist/success")
def show_success_page():
    """
    Todo - shows the success page after playlist saved to spotify
    """
    # spotify_link (link to open playlist via spotify)
    return render_template("/playlist/result.html", result="Success!")


@app.route("/playlist/failure")
def show_failure_page():
    """
    Todo - shows the failure page after playlist not saved to spotify
    """
    return render_template("/playlist/result.html", result="Uh oh!")


#######################
# Custom Error Routes #################
#######################


@app.errorhandler(403)
def forbidden(e):
    return render_template("/errors/403.html"), 403


@app.errorhandler(404)
def page_not_found(e):
    return render_template("/errors/404.html"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("/errors/500.html"), 500
