"""
Web Music Quiz - Flask web interface for the live music quiz
"""

import os
import time
from typing import Dict, Optional

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler

from dotenv import load_dotenv
from rapidfuzz import fuzz

# --- Env & app setup ---
load_dotenv()  # local dev convenience; harmless in prod

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("FLASK_SECRET_KEY", "dev-only-secret")

# In prod, lock this down to your domains if embedding:
socketio = SocketIO(
    app,
    cors_allowed_origins=[
        "http://127.0.0.1:5002",
        "http://localhost:5002",
        "https://mollyrudisill.com",
        "https://www.mollyrudisill.com",
        "https://music-quiz-app-waru.onrender.com",
        "https://quiz.mollyrudisill.com",
    ],
    async_mode="threading"
)

# --- Spotify OAuth factory (session-based token cache) ---
def make_oauth():
    scope = (
        "user-read-currently-playing "
        "user-read-playback-state "
        "user-library-read "
        "user-modify-playback-state"
    )
    cache_handler = FlaskSessionCacheHandler(session)
    return SpotifyOAuth(
        client_id=os.getenv("SPOTIPY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
        scope=scope,
        cache_handler=cache_handler,
        show_dialog=False,
        open_browser=False,
    )

# --- Quiz logic that fetches a per-request Spotify client ---
class WebMusicQuiz:
    """Web version of the music quiz (per-request Spotify client)."""
    def __init__(self):
        pass  # no global Spotipy client; we bind per request

    def _get_sp(self):
        """Return a Spotipy client for the current session, or None if not logged in."""
        oauth = make_oauth()
        token = oauth.get_cached_token()
        if not token:
            return None
        # Spotipy will auto-refresh access token via this oauth/cache handler
        return spotipy.Spotify(auth_manager=oauth)

    def get_currently_playing(self) -> Optional[Dict]:
        try:
            sp = self._get_sp()
            if not sp:
                return None
            current = sp.current_playback()
            if not current or not current.get('is_playing', False):
                return None
            item = current.get('item')
            if not item or item.get('type') != 'track':
                return None
            return {
                'id': item['id'],
                'title': item['name'],
                'artist': ', '.join([artist['name'] for artist in item['artists']]),
                'album': item['album']['name'],
                'year': item['album']['release_date'][:4] if item['album']['release_date'] else 'Unknown',
                'duration_ms': item['duration_ms'],
                'popularity': item['popularity'],
                'progress_ms': current.get('progress_ms', 0),
                'device': current.get('device', {}).get('name', 'Unknown Device'),
                'image_url': item['album']['images'][0]['url'] if item['album']['images'] else None
            }
        except Exception as e:
            print(f"Error getting currently playing: {e}")
            return None

    def skip_to_next_track(self) -> bool:
        try:
            sp = self._get_sp()
            if not sp:
                return False
            sp.next_track()
            time.sleep(1.5)
            return True
        except Exception as e:
            print(f"Error skipping track: {e}")
            return False

    def calculate_score(self, user_title: str, user_artist: str, correct_title: str, correct_artist: str, response_time: float = None):
        """Calculate quiz score (title + artist = 100 points, no speed bonus)."""
        title_similarity = fuzz.ratio(user_title.lower().strip(), correct_title.lower().strip())
        artist_similarity = fuzz.ratio(user_artist.lower().strip(), correct_artist.lower().strip())

        title_points = 0
        artist_points = 0

        # Title scoring (max 70 points)
        if title_similarity >= 90:
            title_points = 70
        elif title_similarity >= 70:
            title_points = 50
        elif title_similarity >= 50:
            title_points = 30

        # Artist scoring (max 30 points)
        if artist_similarity >= 90:
            artist_points = 30
        elif artist_similarity >= 70:
            artist_points = 20
        elif artist_similarity >= 50:
            artist_points = 10

        return {
            'total_points': title_points + artist_points,
            'title_points': title_points,
            'artist_points': artist_points,
            'response_time': response_time,
            'time_message': "",
            'title_similarity': title_similarity,
            'artist_similarity': artist_similarity
        }

# Single quiz instance for utility methods
quiz = WebMusicQuiz()

# --- Routes ---
@app.route('/')
def index():
    oauth = make_oauth()
    if not oauth.get_cached_token():
        return redirect(url_for("login"))
    return render_template('updated.html')

@app.route('/login')
def login():
    oauth = make_oauth()
    return redirect(oauth.get_authorize_url())

@app.route('/callback')
def callback():
    error = request.args.get("error")
    if error:
        return f"Spotify auth error: {error}", 400
    code = request.args.get("code")
    if not code:
        return "Missing ?code", 400
    oauth = make_oauth()
    oauth.get_access_token(code)  # stores tokens in the Flask session
    return redirect(url_for("index"))

@app.route('/auth-url')
def auth_url():
    oauth = make_oauth()
    return oauth.get_authorize_url()

@app.route('/api/current-track')
def get_current_track():
    # Ensure logged in
    if not make_oauth().get_cached_token():
        return redirect(url_for("login"))
    track = quiz.get_currently_playing()
    if track:
        return jsonify({'success': True, 'track': track})
    else:
        return jsonify({'success': False, 'message': 'No track currently playing'})

@app.route('/api/skip-track', methods=['POST'])
def skip_track():
    if not make_oauth().get_cached_token():
        return redirect(url_for("login"))
    success = quiz.skip_to_next_track()
    if success:
        track = quiz.get_currently_playing()
        if track:
            return jsonify({'success': True, 'message': 'Skipped to next track', 'track': track})
        else:
            return jsonify({'success': True, 'message': 'Skipped track, but no new track detected yet'})
    else:
        return jsonify({'success': False, 'message': 'Failed to skip track. Make sure Spotify is playing music.'})

@app.route('/api/submit-guess', methods=['POST'])
def submit_guess():
    # This endpoint doesn't require Spotify
    data = request.json or {}
    user_title = data.get('title', '').strip()
    user_artist = data.get('artist', '').strip()
    correct_title = data.get('correct_title', '')
    correct_artist = data.get('correct_artist', '')
    response_time = data.get('response_time', None)

    if not user_title or not user_artist:
        return jsonify({'success': False, 'message': 'Please provide both title and artist'})

    score = quiz.calculate_score(user_title, user_artist, correct_title, correct_artist, response_time)
    return jsonify({
        'success': True,
        'score': score,
        'user_answer': {'title': user_title, 'artist': user_artist},
        'correct_answer': {'title': correct_title, 'artist': correct_artist}
    })

# --- Socket.IO events ---
@socketio.on('connect')
def handle_connect():
    print('Client connected')
    emit('connected', {'data': 'Connected to quiz server'})

@socketio.on('start_monitoring')
def handle_start_monitoring():
    print('Starting track monitoring')
    emit('monitoring_started', {'message': 'Track monitoring started'})

# --- Entry points ---
if __name__ == '__main__':
    # Local dev server with websockets
    socketio.run(app, debug=True, host='127.0.0.1', port=5002)