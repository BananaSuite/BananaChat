"""BananaChat route registration package."""

from routes.auth import register_auth_routes
from routes.chat import register_chat_routes
from routes.tokens import register_token_routes
from routes.account import register_account_routes
from routes.admin import register_admin_routes
from routes.music import register_music_routes
from routes.errors import register_error_handlers
from routes.api_v1 import register_api_v1
from routes.worker_api import register_worker_api
from routes.access import register_access_routes
from routes.images import register_image_routes
from routes.personalities import register_personality_routes
from routes.customization import register_customization_routes


def register_all_routes(app):
    register_auth_routes(app)
    register_chat_routes(app)
    register_token_routes(app)
    register_account_routes(app)
    register_admin_routes(app)
    register_access_routes(app)
    register_personality_routes(app)
    register_image_routes(app)
    register_music_routes(app)
    register_customization_routes(app)
    register_error_handlers(app)
    register_api_v1(app)
    register_worker_api(app)
