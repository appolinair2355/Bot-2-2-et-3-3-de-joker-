import os
import asyncio
import logging
import sys
from typing import List, Optional, Dict
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    PREDICTION_CHANNEL_ID, PORT, API_POLL_INTERVAL,
    COMPTEUR2_ACTIVE, COMPTEUR2_B, COMPTEUR2_B2, COMPTEUR2_T
)
from utils import get_latest_results

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

client = None
prediction_channel_ok = False
current_game_number = 0
last_prediction_time: Optional[datetime] = None

# Prédictions en attente {pred_game_number: {...}}
pending_predictions: Dict[int, dict] = {}

# Compteur2 — distributions 2/2 et 3/3
compteur2_active = COMPTEUR2_ACTIVE
compteur2_b   = COMPTEUR2_B    # seuil absences 2/2
compteur2_b2  = COMPTEUR2_B2   # seuil absences 3/3
compteur2_t   = COMPTEUR2_T    # décalage prédiction (game courant + T)

compteur2_abs_22  = 0   # absences consécutives de 2/2
compteur2_abs_33  = 0   # absences consécutives de 3/3
compteur2_last_game = 0

# Mode Attente
attente_mode   = False
attente_locked = False

# Historique des prédictions
prediction_history: List[Dict] = []
MAX_HISTORY_SIZE = 100

# Jeux terminés déjà traités par le compteur2
finished_processed_games: set = set()

# Jeux pour lesquels l'absence 2/2 a déjà été comptée en anticipé (joueur a 3 cartes)
early_22_processed_games: set = set()

# Jeux pour lesquels la vérification 3/3 a déjà été faite en anticipé (3 cartes chacun)
early_33_verified_games: set = set()

# Cache des résultats API {game_number: result_dict}
api_results_cache: Dict[int, dict] = {}

# ============================================================================
# UTILITAIRES — Distribution
# ============================================================================

def get_distribution(result: dict) -> Optional[str]:
    """Retourne '2/2', '3/3' ou None selon le nombre final de cartes."""
    if not result.get('is_finished'):
        return None
    p = len(result.get('player_cards', []))
    b = len(result.get('banker_cards', []))
    if p == 2 and b == 2:
        return '2/2'
    if p == 3 and b == 3:
        return '3/3'
    return None   # 2/3 ou 3/2 — distribution mixte

def dist_label(dist: str) -> str:
    """Symbole affiché dans les messages."""
    return '❷/❷' if dist == '2/2' else '❸/❸'

# ============================================================================
# UTILITAIRES — Canaux
# ============================================================================

def normalize_channel_id(channel_id) -> Optional[int]:
    if not channel_id:
        return None
    s = str(channel_id)
    if s.startswith('-100'):
        return int(s)
    if s.startswith('-'):
        return int(s)
    return int(f"-100{s}")

async def resolve_channel(entity_id):
    try:
        if not entity_id:
            return None
        entity = await client.get_entity(normalize_channel_id(entity_id))
        return entity
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

# ============================================================================
# HISTORIQUE DES PRÉDICTIONS
# ============================================================================

def add_prediction_to_history(pred_game: int, dist: str, triggered_at: int):
    global prediction_history
    prediction_history.insert(0, {
        'predicted_game': pred_game,
        'dist': dist,
        'triggered_at': triggered_at,
        'predicted_at': datetime.now(),
        'status': 'en_cours',
        'silent': attente_mode,
    })
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_history_status(pred_game: int, status: str):
    for pred in prediction_history:
        if pred['predicted_game'] == pred_game:
            pred['status'] = status
            break

# ============================================================================
# ENVOI ET MISE À JOUR DES PRÉDICTIONS
# ============================================================================

def build_prediction_msg(pred_game: int, dist: str, result_line: str) -> str:
    label = dist_label(dist)
    return (
        f"📱\U0001d514\U0001d52c\U0001d522\U0001d522\U0001d522\U0001d52f №{pred_game}\n"
        f"⚜️\U0001d516\U0001d531\U0001d51e\U0001d531\U0001d532\U0001d531 {label} \n"
        f"🚦\U0001d513\U0001d52c\U0001d532\U0001d52f\U0001d530\U0001d532\U0001d526\U0001d531\U0001d522 "
        f"\U0001d531\U0001d52f\U0001d52c\U0001d526\U0001d530 \U0001d527\U0001d522\U0001d532\U0001d535\n"
        f"🗯️ \U0001d411\u00e9\U0001d530\U0001d532\U0001d529\U0001d531\U0001d51e\U0001d531\U0001d530:{result_line}"
    )

async def send_prediction(pred_game: int, dist: str, triggered_at: int) -> Optional[int]:
    """Envoie une prédiction 2/2 ou 3/3 au canal."""
    global last_prediction_time, attente_locked

    if not PREDICTION_CHANNEL_ID:
        logger.error("❌ PREDICTION_CHANNEL_ID non configuré")
        return None

    prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
    if not prediction_entity:
        logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
        return None

    msg = build_prediction_msg(pred_game, dist, '⌛')

    try:
        sent = await client.send_message(prediction_entity, msg)
        last_prediction_time = datetime.now()

        pending_predictions[pred_game] = {
            'dist': dist,
            'triggered_at': triggered_at,
            'message_id': sent.id,
            'status': 'en_cours',
            'awaiting_rattrapage': 0,
            'sent_time': datetime.now(),
        }

        add_prediction_to_history(pred_game, dist, triggered_at)

        if attente_mode:
            attente_locked = True

        logger.info(f"✅ Prédiction {dist} envoyée pour #{pred_game} (déclenché au jeu #{triggered_at})")
        return sent.id

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {PREDICTION_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
    return None

async def update_prediction_message(pred_game: int, status_emoji: str, trouve: bool):
    """Met à jour le message de prédiction avec le résultat."""
    global attente_locked

    if pred_game not in pending_predictions:
        return

    pred = pending_predictions[pred_game]
    dist = pred['dist']
    msg_id = pred['message_id']

    new_msg = build_prediction_msg(pred_game, dist, status_emoji)

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal prédiction inaccessible pour mise à jour")
            return

        await client.edit_message(prediction_entity, msg_id, new_msg)
        pred['status'] = status_emoji

        update_prediction_history_status(pred_game, 'gagne' if trouve else 'perdu')

        if trouve:
            logger.info(f"✅ Gagné: #{pred_game} {dist} ({status_emoji})")
        else:
            logger.info(f"❌ Perdu: #{pred_game} {dist}")
            if attente_mode:
                attente_locked = False
                logger.info("🔓 Mode Attente: PERDU → prêt pour prochaine prédiction")

        del pending_predictions[pred_game]

    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# VÉRIFICATION DYNAMIQUE — dès que le jeu est terminé
# ============================================================================

async def check_prediction_result_dynamic(game_number: int, dist: Optional[str]):
    """Vérifie toutes les prédictions en attente concernant ce jeu terminé.

    La vérification porte jusqu'à jeu prédit + 3 (3 rattrapages max).
    dist peut être '2/2', '3/3' ou None (distribution mixte = échec).
    """

    # --- Vérification directe (game_number est le jeu prédit) ---
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('awaiting_rattrapage', 0) == 0:
            target = pred['dist']
            if dist == target:
                logger.info(f"🔍 [DYN] #{game_number}: {target} ✅ direct")
                await update_prediction_message(game_number, '✅0️⃣', True)
            else:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"🔍 [DYN] #{game_number}: {target} ❌ → attente R1 (#{game_number + 1})")
            return

    # --- Vérification rattrapages (game_number est un jeu de rattrapage) ---
    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting <= 0:
            continue
        if game_number != original_game + awaiting:
            continue

        target = pred['dist']
        if dist == target:
            status = f'✅{awaiting}️⃣'
            logger.info(f"🔍 [DYN] R{awaiting} #{game_number}: {target} ✅")
            await update_prediction_message(original_game, status, True)
        else:
            if awaiting < 3:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"🔍 [DYN] R{awaiting} #{game_number}: {target} ❌ → R{awaiting+1} (#{original_game + awaiting + 1})")
            else:
                logger.info(f"🔍 [DYN] R3 #{game_number}: {target} ❌ → PERDU")
                await update_prediction_message(original_game, '❌', False)
        return

# ============================================================================
# COMPTEUR2 — compte 2/2 et 3/3
# ============================================================================

def get_compteur2_status_text() -> str:
    status = "✅ ON" if compteur2_active else "❌ OFF"
    last_game_str = f"#{compteur2_last_game}" if compteur2_last_game else "Aucun"

    bar_22 = '[' + '█' * compteur2_abs_22 + '░' * max(0, compteur2_b  - compteur2_abs_22) + ']'
    bar_33 = '[' + '█' * compteur2_abs_33 + '░' * max(0, compteur2_b2 - compteur2_abs_33) + ']'

    lines = [
        f"📊 Compteur2: {status}",
        f"🎮 Dernier jeu traité: {last_game_str}",
        f"⏭️  Décalage prédiction: T={compteur2_t}",
        "",
        "Absences consécutives:",
        f"❷/❷ : {bar_22} {compteur2_abs_22}/{compteur2_b} (B)",
        f"❸/❸ : {bar_33} {compteur2_abs_33}/{compteur2_b2} (B2)",
    ]

    if attente_mode:
        attente_status = "🔒 Verrouillé (attend PERDU)" if attente_locked else "🔓 Prêt"
        lines.append(f"\n🕐 Mode Attente: ✅ ON | {attente_status}")
    else:
        lines.append(f"\n🕐 Mode Attente: ❌ OFF")

    return "\n".join(lines)

async def process_early_22(game_number: int):
    """Compte l'absence 2/2 en anticipé dès que le joueur prend une 3ème carte.

    Appelé AVANT la fin du jeu — on sait déjà que ce jeu ne sera pas 2/2.
    Évite le double-comptage quand le jeu se termine ensuite.
    """
    global compteur2_abs_22, compteur2_last_game

    if not compteur2_active:
        return

    if game_number in early_22_processed_games:
        return

    early_22_processed_games.add(game_number)
    if len(early_22_processed_games) > 500:
        oldest = min(early_22_processed_games)
        early_22_processed_games.discard(oldest)

    pred_game = game_number + compteur2_t

    compteur2_abs_22 += 1
    logger.info(
        f"📊 [ANTICIPÉ] Compteur2 ❷/❷: absence {compteur2_abs_22}/{compteur2_b} "
        f"(jeu #{game_number}, joueur a déjà 3 cartes → pas de 2/2)"
    )

    if compteur2_abs_22 >= compteur2_b:
        if attente_mode and attente_locked:
            logger.info(f"🔒 Mode Attente: B atteint pour ❷/❷ → prédiction ignorée")
        else:
            logger.info(f"🔮 [ANTICIPÉ] Compteur2: ❷/❷ absent {compteur2_b}x → prédiction pour #{pred_game}")
            await send_prediction(pred_game, '2/2', game_number)
        compteur2_abs_22 = 0


async def process_compteur2(game_number: int, dist: Optional[str]):
    """Traite le Compteur2 pour un jeu terminé.

    dist = '2/2' | '3/3' | None (mixte)
    — Incrémente l'absence du type non observé.
    — Remet à 0 le type observé.
    — Déclenche une prédiction si le seuil B (2/2) ou B2 (3/3) est atteint.
    Si l'absence 2/2 a déjà été comptée en anticipé, on saute cette partie.
    """
    global compteur2_abs_22, compteur2_abs_33, compteur2_last_game

    if not compteur2_active:
        return

    if game_number in finished_processed_games:
        return

    finished_processed_games.add(game_number)
    if len(finished_processed_games) > 500:
        oldest = min(finished_processed_games)
        finished_processed_games.discard(oldest)

    compteur2_last_game = game_number
    pred_game = game_number + compteur2_t

    # ---------- 2/2 ----------
    # Si déjà traité en anticipé (joueur avait 3 cartes avant fin), on ne recompte pas
    if game_number not in early_22_processed_games:
        if dist == '2/2':
            logger.info(f"📊 Compteur2 ❷/❷: trouvé au jeu #{game_number} → reset (était {compteur2_abs_22})")
            compteur2_abs_22 = 0
        else:
            compteur2_abs_22 += 1
            logger.info(f"📊 Compteur2 ❷/❷: absence {compteur2_abs_22}/{compteur2_b} (jeu #{game_number})")
            if compteur2_abs_22 >= compteur2_b:
                if attente_mode and attente_locked:
                    logger.info(f"🔒 Mode Attente: B atteint pour ❷/❷ → prédiction ignorée")
                else:
                    logger.info(f"🔮 Compteur2: ❷/❷ absent {compteur2_b}x → prédiction pour #{pred_game}")
                    await send_prediction(pred_game, '2/2', game_number)
                compteur2_abs_22 = 0
    else:
        # Déjà compté en anticipé — juste réinitialiser si c'était un 2/2 (rare, cas de correction API)
        if dist == '2/2':
            logger.info(f"📊 Compteur2 ❷/❷: jeu #{game_number} anticipé mais finalement 2/2 → reset")
            compteur2_abs_22 = 0

    # ---------- 3/3 ----------
    if dist == '3/3':
        logger.info(f"📊 Compteur2 ❸/❸: trouvé au jeu #{game_number} → reset (était {compteur2_abs_33})")
        compteur2_abs_33 = 0
    else:
        compteur2_abs_33 += 1
        logger.info(f"📊 Compteur2 ❸/❸: absence {compteur2_abs_33}/{compteur2_b2} (jeu #{game_number})")
        if compteur2_abs_33 >= compteur2_b2:
            if attente_mode and attente_locked:
                logger.info(f"🔒 Mode Attente: B2 atteint pour ❸/❸ → prédiction ignorée")
            else:
                logger.info(f"🔮 Compteur2: ❸/❸ absent {compteur2_b2}x → prédiction pour #{pred_game}")
                await send_prediction(pred_game, '3/3', game_number)
            compteur2_abs_33 = 0

# ============================================================================
# BOUCLE DE POLLING API — DYNAMIQUE
# ============================================================================

async def api_polling_loop():
    """Interroge l'API 1xBet en continu.

    Pour chaque jeu terminé (is_finished=True) :
    1. Détermine la distribution (2/2, 3/3 ou mixte).
    2. Vérification dynamique des prédictions en attente.
    3. Mise à jour du Compteur2.
    """
    global current_game_number, api_results_cache

    loop = asyncio.get_event_loop()
    logger.info(f"🔄 Polling API démarré (intervalle: {API_POLL_INTERVAL}s)")

    while True:
        try:
            results = await loop.run_in_executor(None, get_latest_results)

            if results:
                for result in results:
                    game_number  = result["game_number"]
                    is_finished  = result["is_finished"]
                    api_results_cache[game_number] = result

                    p = len(result.get('player_cards', []))
                    b = len(result.get('banker_cards', []))

                    if not is_finished:
                        # ── ANTICIPATION 2/2 ──────────────────────────────────────
                        # Dès que le joueur a 3 cartes, on sait que ce jeu n'est PAS 2/2
                        # → compter l'absence immédiatement et déclencher la prédiction si nécessaire
                        if p >= 3 and game_number not in early_22_processed_games:
                            logger.info(
                                f"🃏 [ANTICIPÉ] Jeu #{game_number} en cours | "
                                f"Joueur: {p} cartes → pas de 2/2 → traitement anticipé"
                            )
                            await process_early_22(game_number)

                        # ── VÉRIFICATION 3/3 ANTICIPÉE ───────────────────────────
                        # Dès que joueur ET banquier ont chacun 3 cartes (avant fin officielle),
                        # on sait que c'est 3/3 → vérifier la prédiction immédiatement
                        if p == 3 and b == 3 and game_number not in early_33_verified_games:
                            early_33_verified_games.add(game_number)
                            if len(early_33_verified_games) > 500:
                                early_33_verified_games.discard(min(early_33_verified_games))
                            logger.info(
                                f"🃏 [ANTICIPÉ 3/3] Jeu #{game_number} en cours | "
                                f"Joueur: 3 cartes | Banquier: 3 cartes → vérification 3/3 immédiate"
                            )
                            await check_prediction_result_dynamic(game_number, '3/3')

                        continue   # attendre la fin pour le reste du traitement

                    if game_number in finished_processed_games:
                        continue   # déjà traité

                    current_game_number = game_number
                    dist = get_distribution(result)

                    logger.info(
                        f"🃏 Jeu #{game_number} terminé | "
                        f"Joueur: {p} cartes | Banquier: {b} cartes | "
                        f"Distribution: {dist or 'mixte'} | Gagnant: {result.get('winner')}"
                    )

                    # 1. Vérification prédictions en attente
                    # (seulement si la vérif 3/3 n'a pas déjà été faite en anticipé)
                    if game_number not in early_33_verified_games:
                        await check_prediction_result_dynamic(game_number, dist)

                    # 2. Compteur2 (la partie 2/2 saute si déjà comptée en anticipé)
                    await process_compteur2(game_number, dist)

                # Nettoyage cache
                if len(api_results_cache) > 300:
                    oldest = min(api_results_cache.keys())
                    del api_results_cache[oldest]

        except Exception as e:
            logger.error(f"❌ Erreur polling API: {e}")
            import traceback
            logger.error(traceback.format_exc())

        await asyncio.sleep(API_POLL_INTERVAL)

# ============================================================================
# RESET AUTOMATIQUE
# ============================================================================

async def auto_reset_system():
    while True:
        try:
            now = datetime.now()
            if now.hour == 1 and now.minute == 0:
                logger.info("🕐 Reset automatique 1h00")
                await perform_full_reset("Reset automatique 1h00")
                await asyncio.sleep(60)
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time
    global compteur2_abs_22, compteur2_abs_33, compteur2_last_game
    global attente_locked, finished_processed_games, api_results_cache
    global early_22_processed_games, early_33_verified_games

    stats = len(pending_predictions)
    pending_predictions.clear()
    last_prediction_time = None
    compteur2_abs_22 = 0
    compteur2_abs_33 = 0
    compteur2_last_game = 0
    attente_locked = False
    finished_processed_games = set()
    early_22_processed_games = set()
    early_33_verified_games = set()
    api_results_cache = {}

    logger.info(f"🔄 {reason} - {stats} prédictions cleared")

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
                f"🔄 **RESET SYSTÈME**\n\n{reason}\n\n"
                f"✅ Compteurs remis à zéro\n"
                f"✅ {stats} prédictions cleared\n\n"
                f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲"
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_compteur2(event):
    global compteur2_active, compteur2_b, compteur2_b2, compteur2_t
    global compteur2_abs_22, compteur2_abs_33, finished_processed_games
    global early_22_processed_games, early_33_verified_games

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_compteur2_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur2_active = True
        compteur2_abs_22 = 0
        compteur2_abs_33 = 0
        finished_processed_games = set()
        early_22_processed_games = set()
        early_33_verified_games = set()
        await event.respond(f"✅ Compteur2 ACTIVÉ\n\n" + get_compteur2_status_text())

    elif arg == 'off':
        compteur2_active = False
        await event.respond("❌ Compteur2 DÉSACTIVÉ")

    elif arg == 'reset':
        compteur2_abs_22 = 0
        compteur2_abs_33 = 0
        finished_processed_games = set()
        early_22_processed_games = set()
        early_33_verified_games = set()
        await event.respond("🔄 Compteur2 remis à zéro\n\n" + get_compteur2_status_text())

    elif arg == 'b':
        # B : seuil absences 2/2
        if len(parts) < 3:
            await event.respond("Usage: `/compteur2 b <valeur>`")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 50:
                await event.respond("❌ B doit être entre 1 et 50")
                return
            old = compteur2_b
            compteur2_b = val
            compteur2_abs_22 = 0
            await event.respond(f"✅ B (❷/❷): {old} → {compteur2_b}\n\n" + get_compteur2_status_text())
        except ValueError:
            await event.respond("❌ Valeur invalide")

    elif arg == 'b2':
        # B2 : seuil absences 3/3
        if len(parts) < 3:
            await event.respond("Usage: `/compteur2 b2 <valeur>`")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 50:
                await event.respond("❌ B2 doit être entre 1 et 50")
                return
            old = compteur2_b2
            compteur2_b2 = val
            compteur2_abs_33 = 0
            await event.respond(f"✅ B2 (❸/❸): {old} → {compteur2_b2}\n\n" + get_compteur2_status_text())
        except ValueError:
            await event.respond("❌ Valeur invalide")

    elif arg == 't':
        # T : décalage du jeu prédit
        if len(parts) < 3:
            await event.respond("Usage: `/compteur2 t <valeur>`")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 20:
                await event.respond("❌ T doit être entre 1 et 20")
                return
            old = compteur2_t
            compteur2_t = val
            await event.respond(f"✅ T: {old} → {compteur2_t} | Prédiction pour jeu courant+{compteur2_t}")
        except ValueError:
            await event.respond("❌ Valeur invalide")

    else:
        await event.respond(
            "📊 **COMPTEUR2 - Aide**\n\n"
            "`/compteur2` — Afficher l'état\n"
            "`/compteur2 on` — Activer\n"
            "`/compteur2 off` — Désactiver\n"
            "`/compteur2 reset` — Remettre les compteurs à zéro\n"
            "`/compteur2 b <val>` — Seuil absences ❷/❷ (B)\n"
            "`/compteur2 b2 <val>` — Seuil absences ❸/❸ (B2)\n"
            "`/compteur2 t <val>` — Décalage jeu prédit (T)"
        )

async def cmd_attente(event):
    global attente_mode, attente_locked

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        mode_str = "✅ ON" if attente_mode else "❌ OFF"
        lock_str = "🔒 Verrouillé (attend PERDU)" if (attente_mode and attente_locked) else "🔓 Prêt"
        await event.respond(
            f"🕐 **MODE ATTENTE**\n\nStatut: {mode_str}\nÉtat: {lock_str}\n\n"
            f"`/attente on` — Activer\n"
            f"`/attente off` — Désactiver\n"
            f"`/attente reset` — Déverrouiller manuellement"
        )
        return

    arg = parts[1].lower()
    if arg == 'on':
        attente_mode = True
        attente_locked = False
        await event.respond("✅ **Mode Attente ACTIVÉ**")
    elif arg == 'off':
        attente_mode = False
        attente_locked = False
        await event.respond("❌ **Mode Attente DÉSACTIVÉ**")
    elif arg == 'reset':
        attente_locked = False
        await event.respond(f"🔓 Mode Attente déverrouillé | Mode: {'✅ ON' if attente_mode else '❌ OFF'}")
    else:
        await event.respond("`/attente on|off|reset`")

async def cmd_history(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    if not prediction_history:
        await event.respond("📜 Aucune prédiction dans l'historique.")
        return

    lines = ["📜 **HISTORIQUE DES PRÉDICTIONS**", "═══════════════════════════════════════", ""]

    for i, pred in enumerate(prediction_history[:20], 1):
        p_game = pred['predicted_game']
        d = pred.get('dist', '?')
        trig = pred.get('triggered_at', '?')
        time_str = pred['predicted_at'].strftime('%H:%M:%S')
        silent_tag = " [Attente]" if pred.get('silent') else ""

        status = pred['status']
        status_str = "⏳ En cours..." if status == 'en_cours' else ("✅ GAGNÉ" if status == 'gagne' else ("❌ PERDU" if status == 'perdu' else f"❓ {status}"))

        lines.append(
            f"{i}. 🕐 `{time_str}` | **Game #{p_game}** {dist_label(d)}{silent_tag}\n"
            f"   📉 Déclenché au jeu #{trig}\n"
            f"   📊 Résultat: {status_str}"
        )
        lines.append("")

    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            d = pred.get('dist', '?')
            ar = pred.get('awaiting_rattrapage', 0)
            st = f"Attente R{ar} (#{num + ar})" if ar > 0 else "Vérification directe"
            lines.append(f"• Game #{num} {dist_label(d)}: {st}")

    lines.append("═══════════════════════════════════════")
    await event.respond("\n".join(lines))

async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    pred_status = "❌"
    pred_name = "Inaccessible"
    try:
        if PREDICTION_CHANNEL_ID:
            pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
            if pred_entity:
                pred_status = "✅"
                pred_name = getattr(pred_entity, 'title', 'Sans titre')
    except Exception as e:
        pred_status = f"❌ ({str(e)[:30]})"

    await event.respond(
        f"📡 **CONFIGURATION**\n\n"
        f"**Source:** API 1xBet (polling {API_POLL_INTERVAL}s)\n"
        f"**Jeux en cache:** {len(api_results_cache)} | **Traités:** {len(finished_processed_games)}\n\n"
        f"**Canal Prédiction:**\n"
        f"ID: `{PREDICTION_CHANNEL_ID}`\n"
        f"Status: {pred_status} | Nom: {pred_name}\n\n"
        f"**Paramètres Compteur2:**\n"
        f"B={compteur2_b} (❷/❷) | B2={compteur2_b2} (❸/❸) | T={compteur2_t}\n"
        f"Actif: {'✅' if compteur2_active else '❌'} | Mode Attente: {'✅' if attente_mode else '❌'}\n"
        f"Admin ID: `{ADMIN_ID}`"
    )

async def cmd_test(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🧪 Test de connexion au canal de prédiction...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ PREDICTION_CHANNEL_ID non configuré")
            return

        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond(f"❌ Canal inaccessible `{PREDICTION_CHANNEL_ID}`")
            return

        sent = await client.send_message(prediction_entity, build_prediction_msg(9999, '2/2', '⌛') + f"\n🕐 {datetime.now().strftime('%H:%M:%S')} [TEST]")
        await asyncio.sleep(2)
        await client.edit_message(prediction_entity, sent.id, build_prediction_msg(9999, '2/2', '✅0️⃣') + f"\n🕐 {datetime.now().strftime('%H:%M:%S')} [TEST]")
        await asyncio.sleep(2)
        await client.delete_messages(prediction_entity, [sent.id])

        pred_name_display = getattr(prediction_entity, 'title', str(prediction_entity.id))
        await event.respond(f"✅ **TEST RÉUSSI!**\n\nCanal: `{pred_name_display}`\nEnvoi, modification et suppression: OK")

    except ChatWriteForbiddenError:
        await event.respond("❌ Permission refusée — ajoutez le bot comme administrateur.")
    except Exception as e:
        await event.respond(f"❌ Échec: {e}")

async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel admin")
    await event.respond("✅ Reset effectué! Compteurs remis à zéro.")

async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    lines = [
        "📈 **ÉTAT DU BOT**",
        "",
        get_compteur2_status_text(),
        "",
        f"🔮 Prédictions actives: {len(pending_predictions)}",
        f"📡 Source: API 1xBet (polling {API_POLL_INTERVAL}s)",
    ]

    if pending_predictions:
        lines.append("")
        for num, pred in sorted(pending_predictions.items()):
            d = pred.get('dist', '?')
            ar = pred.get('awaiting_rattrapage', 0)
            st = f"R{ar} (#{num+ar})" if ar > 0 else "Direct"
            lines.append(f"• Game #{num} {dist_label(d)}: {st}")

    await event.respond("\n".join(lines))

async def cmd_announce(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("Usage: `/announce Message`")
        return

    text = parts[1].strip()
    if len(text) > 500:
        await event.respond("❌ Trop long (max 500 caractères)")
        return

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond("❌ Canal de prédiction non accessible")
            return
        now = datetime.now()
        msg = (
            f"╔══════════════════════════════════════╗\n"
            f"║     📢 ANNONCE OFFICIELLE 📢          ║\n"
            f"╠══════════════════════════════════════╣\n\n"
            f"{text}\n\n"
            f"╠══════════════════════════════════════╣\n"
            f"║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}\n"
            f"╚══════════════════════════════════════╝\n\n"
            f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲"
        )
        sent = await client.send_message(prediction_entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "📖 **BACCARAT PREMIUM+2 - AIDE**\n\n"
        "**🎮 Système de prédiction (Compteur2):**\n"
        "• Compte les absences consécutives de ❷/❷ et ❸/❸\n"
        "• ❷/❷ = joueur 2 cartes ET banquier 2 cartes\n"
        "• ❸/❸ = joueur 3 cartes ET banquier 3 cartes\n"
        "• Absence ❷/❷ ≥ B → prédit ❷/❷ pour jeu+T\n"
        "• Absence ❸/❸ ≥ B2 → prédit ❸/❸ pour jeu+T\n"
        "• Vérification jusqu'au jeu prédit +3 (3 rattrapages)\n\n"
        "**🔧 Commandes Admin:**\n"
        "`/compteur2` — État\n"
        "`/compteur2 on/off/reset` — Contrôle\n"
        "`/compteur2 b <val>` — Seuil B pour ❷/❷\n"
        "`/compteur2 b2 <val>` — Seuil B2 pour ❸/❸\n"
        "`/compteur2 t <val>` — Décalage T\n"
        "`/attente on/off/reset` — Mode Attente\n"
        "`/status` — État complet\n"
        "`/history` — Historique\n"
        "`/channels` — Configuration\n"
        "`/test` — Tester le canal\n"
        "`/reset` — Reset complet\n"
        "`/announce <msg>` — Annonce\n"
        "`/help` — Cette aide"
    )

# ============================================================================
# CONFIGURATION DES HANDLERS
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    client.add_event_handler(cmd_attente,   events.NewMessage(pattern=r'^/attente'))
    client.add_event_handler(cmd_status,    events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history,   events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_help,      events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset,     events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels,  events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test,      events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_announce,  events.NewMessage(pattern=r'^/announce'))

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    session = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()

        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal: {e}")

        logger.info(
            f"🤖 Bot démarré | B={compteur2_b} (❷/❷) | B2={compteur2_b2} (❸/❸) | "
            f"T={compteur2_t} | Attente={'ON' if attente_mode else 'OFF'}"
        )
        return True

    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return

        asyncio.create_task(auto_reset_system())
        asyncio.create_task(api_polling_loop())
        logger.info("🔄 Auto-reset et polling API démarrés")

        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT PREMIUM+2 🎲 Running"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"🌐 Serveur web démarré sur port {PORT}")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
