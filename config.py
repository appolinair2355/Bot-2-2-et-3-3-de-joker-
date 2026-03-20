# config.py
"""
Configuration BACCARAT AI 🤖
"""

import os

def parse_channel_id(env_var: str, default: str) -> int:
    """Parse l'ID de canal Telegram."""
    value = os.getenv(env_var) or default
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        return int(default)

# Canal de prédiction
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003416207527')

# Authentification
ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')
API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
TELEGRAM_SESSION = os.getenv('TELEGRAM_SESSION', '')

# Serveur
PORT = int(os.getenv('PORT') or '5000')

# Polling API (secondes entre chaque appel)
API_POLL_INTERVAL = int(os.getenv('API_POLL_INTERVAL') or '5')

# Compteur2 - distributions 2/2 et 3/3
COMPTEUR2_ACTIVE = os.getenv('COMPTEUR2_ACTIVE', 'true').lower() == 'true'

# B  : seuil d'absences consécutives de 2/2 avant prédiction 2/2
COMPTEUR2_B  = int(os.getenv('COMPTEUR2_B')  or '4')

# B2 : seuil d'absences consécutives de 3/3 avant prédiction 3/3
COMPTEUR2_B2 = int(os.getenv('COMPTEUR2_B2') or '4')

# T  : décalage → prédit la distribution pour le jeu (jeu_courant + T)
COMPTEUR2_T  = int(os.getenv('COMPTEUR2_T')  or '1')
