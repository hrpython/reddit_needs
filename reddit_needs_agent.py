"""
reddit_needs_agent.py
Agent qui parcourt Reddit (API officielle OAuth, accès approuvé par Reddit), repère les posts qui expriment
un besoin, fait analyser chaque candidat par un LLM (vLLM local, API compatible OpenAI),
puis classe les besoins pouvant être satisfaits par un moyen numérique.

Usage :
    python reddit_needs_agent.py collect     # récupère les posts candidats
    python reddit_needs_agent.py analyze     # analyse LLM des candidats
    python reddit_needs_agent.py report      # génère le rapport Markdown + CSV
    python reddit_needs_agent.py purge       # supprime les données de plus de 90 jours (auto avant collect)
    python reddit_needs_agent.py all         # enchaîne les trois
"""

import csv
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------------
# CONFIGURATION (à adapter)
# ----------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path):
    """Charge un fichier .env (CLE=valeur) sans écraser les variables déjà définies."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

BASE_DIR = os.environ.get("NEEDS_BASE_DIR", SCRIPT_DIR)
DB_PATH = os.path.join(BASE_DIR, "needs.db")
REPORT_MD = os.path.join(BASE_DIR, "rapport_besoins.md")
REPORT_CSV = os.path.join(BASE_DIR, "rapport_besoins.csv")

# Reddit : API officielle (OAuth), accès soumis à l'approbation de Reddit
# (voir la Responsible Builder Policy). Les identifiants viennent du fichier .env.
# Le User-Agent doit suivre le format imposé par Reddit : <plateforme>:<id app>:<version> (by /u/<pseudo>)
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "windows:needs-finder:v0.1 (by /u/VOTRE_PSEUDO)")
REDDIT_API_BASE = "https://oauth.reddit.com"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
# Pause entre deux requêtes : à régler selon les limites accordées par Reddit.
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "2"))
# Conservation des données Reddit : le texte brut (posts, commentaires) est supprimé dès l'analyse
# terminée, et au plus tard après 48 h (recommandation de Reddit). Les résultats d'analyse et les
# rapports sont conservés RETENTION_DAYS jours, tant que le post source existe encore sur Reddit.
TEXT_RETENTION_HOURS = int(os.environ.get("TEXT_RETENTION_HOURS", "48"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

# LLM : serveur vLLM (API compatible OpenAI), par ex. joignable via Tailscale
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "NOM_DU_MODELE_SERVI_PAR_VLLM")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "EMPTY")

# Subreddits généralistes où les gens expriment des frustrations / besoins
SUBREDDITS = [
    # Entrepreneuriat / business (audience majoritairement US)
    "smallbusiness", "Entrepreneur", "startups", "SaaS", "SideProject",
    "SomebodyMakeThis", "AppIdeas", "freelance", "ecommerce", "shopify", "Etsy",
    # Subreddits intrinsèquement US (fiscalité, droit, santé, immobilier, crédit)
    "personalfinance", "legaladvice", "tax", "taxpros", "Accounting", "Bookkeeping",
    "StudentLoans", "CreditCards", "mortgage", "FirstTimeHomeBuyer", "realtors",
    "RealEstate", "landlord", "Insurance",
    # Métiers et jobs US
    "Teachers", "nursing", "Construction", "HVAC", "Plumbing", "electricians",
    "Truckers", "UberDrivers", "doordash_drivers", "dentistry",
]

# Signaux "États-Unis" et "hors États-Unis" (heuristique : Reddit n'expose pas le pays des auteurs)
US_SIGNALS = re.compile(
    r"\b(IRS|401\(?k\)?|IRA|Roth|FICO|Medicare|Medicaid|HSA|FSA|W-?2|1099|SSN|LLC|S-?corp|"
    r"EIN|Social Security|FAFSA|HOA|DMV|FDA|OSHA|USPS|UPS|FedEx|Walmart|Costco|"
    r"Venmo|Zelle|CashApp|Stripe|QuickBooks|Obamacare|state tax|sales tax|"
    r"in the (US|USA|U\.S\.)|United States|American|"
    r"Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|"
    r"Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
    r"Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    r"New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    r"Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|"
    r"Washington State|West Virginia|Wisconsin|Wyoming)\b|\$\s?\d"
)
NON_US_SIGNALS = re.compile(
    r"(£|€|\b(HMRC|NHS|VAT|GST|ABN|CRA|RRSP|TFSA|Revolut|Aadhaar|PAN card|"
    r"UK|U\.K\.|Britain|England|Scotland|Wales|Ireland|Canada|Canadian|Australia|Australian|"
    r"India|Indian|Germany|France|Spain|Italy|Netherlands|Brazil|Mexico|Philippines|Nigeria|"
    r"South Africa|New Zealand|Singapore|Pakistan|EU|GDPR)\b)"
)

POSTS_PER_SUBREDDIT_NEW = 150
POSTS_PER_SUBREDDIT_TOP_WEEK = 50
MAX_COMMENTS_PER_POST = 25
MIN_COMMENTS_TO_READ = 3
MAX_POSTS_TO_ANALYZE_PER_RUN = 300
MAX_TEXT_CHARS_FOR_LLM = 5000
# Erreurs réseau / serveur LLM : nouvelles tentatives, puis arrêt du run (les posts restent 'pending')
MAX_LLM_RETRIES = 3
MAX_CONSECUTIVE_LLM_FAILURES = 3

NEED_PATTERNS = [
    r"i wish there (was|were)", r"is there (a|an|any) (tool|app|software|service|website|way)",
    r"looking for (a|an|some) (tool|app|software|service|way|solution)",
    r"is there something (that|which)", r"does anyone know (of )?(a|an|any) (tool|app|software)",
    r"alternative to", r"i('m| am) (so )?(tired|sick|frustrated|fed up)",
    r"how do you (deal|handle|manage|keep track)", r"would (gladly )?pay for",
    r"i('d| would) pay", r"shut up and take my money", r"someone (should|needs to) (build|make)",
    r"there should be (a|an)", r"pain point", r"biggest (problem|struggle|headache)",
    r"waste (so much|a lot of) time", r"manually", r"spreadsheet hell",
    r"can't find (a|any) (tool|app|software)", r"no good (tool|app|solution)",
]
NEED_REGEX = re.compile("|".join(NEED_PATTERNS), re.IGNORECASE)

SYSTEM_PROMPT = """Tu es un analyste de marché. On te donne un post Reddit et des commentaires.
Détermine s'il exprime un besoin réel qu'un produit ou service NUMÉRIQUE (app, SaaS, site, automatisation, bot, extension, outil de données) pourrait satisfaire.
Réponds UNIQUEMENT par un objet JSON, sans texte autour, avec ces clés :
{
 "has_need": true/false,
 "need": "besoin reformulé en une phrase (anglais)",
 "category": "catégorie courte en 1 à 3 mots, minuscules (ex: invoicing, scheduling, data cleaning)",
 "audience": "qui a ce besoin",
 "intensity": entier 1-5 (1 = gêne légère, 5 = douleur forte, récurrente),
 "current_solution": "ce qu'ils font aujourd'hui (ou 'none')",
 "willingness_to_pay": entier 0-3 (0 = aucun signe, 3 = dit explicitement qu'il paierait),
 "digital_feasibility": entier 1-5 (5 = un MVP logiciel est simple à construire),
 "evidence": "citation très courte (moins de 15 mots) du post justifiant le besoin"
}
Si has_need est false, mets les autres champs à des valeurs vides ou 0.

Ajoute aussi la clé "us_based" (true/false/null) :
- true si le besoin concerne clairement des personnes ou entreprises aux États-Unis (dollars, IRS, états américains, lois ou services américains, ou contexte typiquement US) ;
- false UNIQUEMENT si le contexte est clairement hors États-Unis (UK, Canada, UE, Inde, etc.) ;
- null si rien n'indique le pays (besoin générique, valable partout)."""


# ----------------------------------------------------------------------------
# BASE DE DONNÉES
# ----------------------------------------------------------------------------
def db_connect():
    os.makedirs(BASE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete=ON")  # les données supprimées sont écrasées, pas seulement déréférencées
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            subreddit TEXT, title TEXT, url TEXT, score INTEGER,
            num_comments INTEGER, created_utc REAL, text TEXT,
            status TEXT DEFAULT 'pending',
            collected_at REAL
        );
        CREATE TABLE IF NOT EXISTS needs (
            post_id TEXT PRIMARY KEY,
            need TEXT, category TEXT, audience TEXT, intensity INTEGER,
            current_solution TEXT, willingness_to_pay INTEGER,
            digital_feasibility INTEGER, evidence TEXT, raw_json TEXT
        );
        """
    )
    # Migration : bases créées avant l'ajout de collected_at (les lignes existantes démarrent à "maintenant")
    columns = [c["name"] for c in conn.execute("PRAGMA table_info(posts)")]
    if "collected_at" not in columns:
        conn.execute("ALTER TABLE posts ADD COLUMN collected_at REAL")
    conn.execute("UPDATE posts SET collected_at=? WHERE collected_at IS NULL", (time.time(),))
    conn.commit()
    return conn


def reddit_ready():
    return bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET) and "VOTRE_PSEUDO" not in REDDIT_USER_AGENT


def is_gone(post):
    """Post supprimé (par son auteur) ou retiré (modération, filtre anti-spam) sur Reddit."""
    return bool(post.get("removed_by_category")) or post.get("selftext") in ("[deleted]", "[removed]")


def verify_deletions(conn):
    """Supprime de la base les posts qui n'existent plus sur Reddit (obligation de la Data API).
    Ne vérifie que les lignes qui portent du contenu ou un résultat dérivé. Renvoie le nombre supprimé."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM posts WHERE status IN ('pending','error','need')")]
    gone = []
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        data = reddit_get(f"{REDDIT_API_BASE}/api/info",
                          {"id": ",".join(f"t3_{x}" for x in batch), "raw_json": 1})
        if not data or "data" not in data:
            print("  ! vérification des suppressions impossible pour un lot, ignoré")
            continue
        alive = {c["data"]["id"]: c["data"] for c in data["data"].get("children", []) if c.get("kind") == "t3"}
        if not alive and len(batch) >= 10:
            print("  ! réponse vide suspecte pour un lot, ignoré")
            continue
        gone += [x for x in batch if x not in alive or is_gone(alive[x])]
    for post_id in gone:
        conn.execute("DELETE FROM needs WHERE post_id=?", (post_id,))
        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
    conn.commit()
    return len(gone)


def purge():
    """Applique la politique de conservation des données Reddit :
    - titres et liens avec titre : jamais conservés ;
    - texte brut des posts et citations : supprimés après TEXT_RETENTION_HOURS (ou dès l'analyse terminée) ;
    - posts supprimés sur Reddit : retirés de la base, ainsi que les rapports qui les mentionnaient ;
    - tout le reste (résultats d'analyse, rapports) : supprimé après RETENTION_DAYS jours."""
    conn = db_connect()
    now = time.time()
    text_cutoff = now - TEXT_RETENTION_HOURS * 3600
    cutoff = now - RETENTION_DAYS * 86400

    scrubbed = conn.execute("UPDATE posts SET title='' WHERE title IS NOT NULL AND title != ''").rowcount
    scrubbed += conn.execute(
        "UPDATE posts SET url='https://www.reddit.com/comments/' || id "
        "WHERE url IS NOT 'https://www.reddit.com/comments/' || id").rowcount
    expired = conn.execute(
        "UPDATE posts SET status='expired' WHERE status='pending' AND collected_at < ?", (text_cutoff,)).rowcount
    scrubbed += conn.execute(
        "UPDATE posts SET text='' WHERE text IS NOT NULL AND text != '' AND collected_at < ?",
        (text_cutoff,)).rowcount
    conn.execute(
        "UPDATE needs SET evidence='', raw_json='' WHERE (evidence != '' OR raw_json != '') "
        "AND post_id IN (SELECT id FROM posts WHERE collected_at < ?)", (text_cutoff,))

    conn.execute("DELETE FROM needs WHERE post_id IN (SELECT id FROM posts WHERE collected_at < ?)", (cutoff,))
    old = conn.execute("DELETE FROM posts WHERE collected_at < ?", (cutoff,)).rowcount
    conn.commit()

    gone = 0
    if reddit_ready():
        gone = verify_deletions(conn)
    else:
        print("[purge] identifiants Reddit absents : vérification des posts supprimés ignorée")

    if scrubbed or old or gone:
        conn.execute("VACUUM")  # le contenu supprimé ne doit pas rester dans le fichier de la base
    conn.close()

    # Les rapports reprennent des résultats d'analyse : même durée de conservation, et supprimés
    # dès qu'un post source disparaît de Reddit (ils se régénèrent avec la commande report)
    removed_reports = 0
    for path in (REPORT_MD, REPORT_CSV):
        if os.path.isfile(path) and (gone or os.path.getmtime(path) < cutoff):
            os.remove(path)
            removed_reports += 1
    print(f"[purge] {old} posts de plus de {RETENTION_DAYS} jours, {gone} posts supprimés sur Reddit, "
          f"{expired} textes non analysés expirés, {removed_reports} rapports supprimés")


# ----------------------------------------------------------------------------
# COLLECTE
# ----------------------------------------------------------------------------
_reddit_token = {"value": None, "expires": 0.0}


def require_reddit_credentials():
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        sys.exit("Identifiants Reddit manquants : renseignez REDDIT_CLIENT_ID et REDDIT_CLIENT_SECRET "
                 "dans le fichier .env (voir .env.example). L'accès à l'API doit être approuvé par Reddit.")
    if "VOTRE_PSEUDO" in REDDIT_USER_AGENT:
        sys.exit("Renseignez REDDIT_USER_AGENT dans .env (ex. : windows:needs-finder:v0.1 (by /u/votre_pseudo)).")


def reddit_access_token():
    """Jeton OAuth (application seule, lecture), renouvelé automatiquement avant expiration."""
    if _reddit_token["value"] and time.time() < _reddit_token["expires"] - 60:
        return _reddit_token["value"]
    r = requests.post(
        REDDIT_TOKEN_URL,
        auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": REDDIT_USER_AGENT},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"jeton Reddit refusé : {data}")
    _reddit_token["value"] = data["access_token"]
    _reddit_token["expires"] = time.time() + as_int(data.get("expires_in"), 3600)
    return _reddit_token["value"]


def reddit_get(url, params=None):
    """GET sur l'API OAuth de Reddit, avec pause et gestion du 429 / 401."""
    for _ in range(5):
        try:
            headers ={"User-Agent": REDDIT_USER_AGENT, "Authorization": f"bearer {reddit_access_token()}"}
            r = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            print(f"  ! réseau ({exc}), nouvel essai dans 15 s")
            time.sleep(15)
            continue

        if r.status_code == 401:
            _reddit_token["value"] = None  # jeton expiré ou invalide : on en redemande un
            continue

        if r.status_code == 200:
            time.sleep(REQUEST_DELAY_SECONDS)
            try:
                return r.json()
            except ValueError:
                print(f"  ! réponse non JSON pour {url}")
                return None
        if r.status_code == 429:
            wait = as_int(r.headers.get("Retry-After"), 60) + 5
            print(f"  ! limite atteinte (429), pause de {wait} s")
            time.sleep(wait)
            continue
        if r.status_code in (403, 404):
            print(f"  ! accès refusé ou introuvable ({r.status_code}) : {url}")
            return None
        print(f"  ! HTTP {r.status_code}, nouvel essai dans 15 s")
        time.sleep(15)
    return None


def fetch_listing(sub_name, sort, total, **extra):
    """Récupère jusqu'à `total` posts d'un subreddit (pages de 100 max)."""
    posts, after = [], None
    while len(posts) < total:
        params = {"limit": min(100, total - len(posts)), "raw_json": 1, **extra}
        if after:
            params["after"] = after
        data = reddit_get(f"{REDDIT_API_BASE}/r/{sub_name}/{sort}", params)
        if not data:
            break
        children = data.get("data", {}).get("children", [])
        posts += [c["data"] for c in children if c.get("kind") == "t3"]
        after = data.get("data", {}).get("after")
        if not after or not children:
            break
    return posts


def fetch_comments(post_id):
    """Commentaires de premier niveau d'un post (les mieux notés d'abord)."""
    data = reddit_get(
        f"{REDDIT_API_BASE}/comments/{post_id}",
        {"limit": MAX_COMMENTS_PER_POST, "depth": 1, "sort": "top", "raw_json": 1},
    )
    if not data or len(data) < 2:
        return []
    out = []
    for c in data[1].get("data", {}).get("children", []):
        body = c.get("data", {}).get("body") if c.get("kind") == "t1" else None
        if body and body not in ("[deleted]", "[removed]"):
            out.append(body)
    return out[:MAX_COMMENTS_PER_POST]


def save_post(conn, s, sub_name, status, text=""):
    """Mémorise un post. Les posts écartés sont gardés (sans texte) pour ne pas être re-téléchargés."""
    conn.execute(
        "INSERT OR IGNORE INTO posts (id, subreddit, title, url, score, num_comments, created_utc, text, status, "
        "collected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        # Pas de titre ni de permalien (qui contient le titre) : seul l'identifiant sert à retrouver le post
        (s["id"], sub_name, "", f"https://www.reddit.com/comments/{s['id']}",
         s.get("score", 0), s.get("num_comments", 0), s.get("created_utc", 0), text, status, time.time()),
    )


def collect():
    require_reddit_credentials()
    purge()
    conn = db_connect()
    added = 0

    for sub_name in SUBREDDITS:
        print(f"[collect] r/{sub_name}")
        submissions = fetch_listing(sub_name, "new", POSTS_PER_SUBREDDIT_NEW)
        submissions += fetch_listing(sub_name, "top", POSTS_PER_SUBREDDIT_TOP_WEEK, t="week")
        if not submissions:
            print(f"  ! aucun post récupéré pour r/{sub_name}")
            continue

        seen = set()
        for s in submissions:
            sid = s.get("id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            if conn.execute("SELECT 1 FROM posts WHERE id=?", (sid,)).fetchone():
                continue

            title = s.get("title", "")
            selftext = s.get("selftext") or ""
            if selftext in ("[removed]", "[deleted]"):
                selftext = ""
            score = s.get("score", 0)
            num_comments = s.get("num_comments", 0)

            body = f"{title}\n{selftext}"
            matched = bool(NEED_REGEX.search(body))
            comments_text = ""

            if num_comments >= MIN_COMMENTS_TO_READ and (matched or score >= 20):
                comments_text = "\n---\n".join(fetch_comments(sid))
                if not matched and NEED_REGEX.search(comments_text):
                    matched = True

            if not matched:
                save_post(conn, s, sub_name, "skipped")
                continue

            # Filtre États-Unis : on écarte les posts avec signaux hors US sans aucun signal US
            check_text = body + "\n" + comments_text
            if NON_US_SIGNALS.search(check_text) and not US_SIGNALS.search(check_text):
                save_post(conn, s, sub_name, "non_us")
                continue

            full_text = (body + "\n\nCOMMENTAIRES:\n" + comments_text)[:MAX_TEXT_CHARS_FOR_LLM]
            save_post(conn, s, sub_name, "pending", full_text)
            added += 1
        conn.commit()

    print(f"[collect] terminé : {added} nouveaux candidats")


# ----------------------------------------------------------------------------
# ANALYSE LLM
# ----------------------------------------------------------------------------
def call_llm(text):
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "max_tokens": 1500,
        # Modèles Qwen3 : désactive la phase de réflexion, qui consommerait les jetons
        # et tronquerait le JSON (constaté : réponse coupée à max_tokens).
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
    r = requests.post(f"{LLM_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=180)
    r.raise_for_status()
    try:
        choice = r.json()["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError("réponse du LLM illisible")
    if choice.get("finish_reason") == "length":
        raise ValueError("réponse du LLM tronquée (finish_reason=length)")
    return choice["message"].get("content") or ""


class LLMUnavailable(Exception):
    """Le serveur LLM ne répond pas (réseau, timeout, 5xx) : le post lui-même n'est pas en cause."""


def call_llm_retry(text):
    """call_llm avec nouvelles tentatives sur les erreurs transitoires.
    Lève LLMUnavailable si le serveur reste injoignable, ValueError si la requête est rejetée (4xx)."""
    last = None
    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            return call_llm(text)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status < 500 and status not in (408, 429):
                raise ValueError(f"HTTP {status} du serveur LLM")
            last = exc
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        if attempt < MAX_LLM_RETRIES:
            time.sleep(5 * attempt)
    raise LLMUnavailable(str(last))


def parse_json(raw):
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("pas de JSON dans la réponse")
    return json.loads(match.group(0))


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value):
    """Booléen tolérant : le LLM peut renvoyer "false" / "null" en chaîne."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "oui", "1")
    return bool(value)


def analyze():
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM posts WHERE status='pending' ORDER BY score DESC LIMIT ?",
        (MAX_POSTS_TO_ANALYZE_PER_RUN,),
    ).fetchall()
    print(f"[analyze] {len(rows)} posts à analyser")

    failures = 0
    for i, row in enumerate(rows, 1):
        try:
            raw = call_llm_retry(row["text"])
            failures = 0
            data = parse_json(raw)
        except LLMUnavailable as exc:
            # Serveur injoignable : le post reste 'pending' pour un prochain run
            failures += 1
            print(f"  ! LLM injoignable ({exc}) [{failures}/{MAX_CONSECUTIVE_LLM_FAILURES}]")
            if failures >= MAX_CONSECUTIVE_LLM_FAILURES:
                print(f"[analyze] arrêt : serveur LLM indisponible, {len(rows) - i + 1} posts restent en attente")
                return
            continue
        except Exception as exc:
            failures = 0
            print(f"  ! {row['id']} : {exc}")
            conn.execute("UPDATE posts SET status='error' WHERE id=?", (row["id"],))
            conn.commit()
            continue

        # Hors US seulement si le LLM l'affirme (false) ; null / absent = inconnu, on garde
        non_us = "us_based" in data and data["us_based"] is not None and not as_bool(data["us_based"])
        if as_bool(data.get("has_need")) and non_us:
            conn.execute("UPDATE posts SET status='non_us', text='' WHERE id=?", (row["id"],))
        elif as_bool(data.get("has_need")):
            conn.execute(
                "INSERT OR REPLACE INTO needs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], data.get("need", ""), (data.get("category") or "other").lower().strip(),
                    data.get("audience", ""), as_int(data.get("intensity")),
                    data.get("current_solution", ""), as_int(data.get("willingness_to_pay")),
                    as_int(data.get("digital_feasibility")), data.get("evidence", ""),
                    json.dumps(data, ensure_ascii=False),
                ),
            )
            conn.execute("UPDATE posts SET status='need', text='' WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE posts SET status='no_need', text='' WHERE id=?", (row["id"],))
        conn.commit()
        if i % 10 == 0:
            print(f"  {i}/{len(rows)}")

    print("[analyze] terminé")


# ----------------------------------------------------------------------------
# RAPPORT
# ----------------------------------------------------------------------------
def need_score(r):
    """Score d'un besoin individuel."""
    traction = math.log1p(max(r["score"], 0)) + math.log1p(max(r["num_comments"], 0))
    return (r["intensity"] * (1 + r["willingness_to_pay"]) * r["digital_feasibility"]) * (1 + traction / 5)


def report():
    purge()  # le rapport ne doit jamais citer un post supprimé sur Reddit ni des données expirées
    conn = db_connect()
    rows = conn.execute(
        "SELECT n.*, p.subreddit, p.url, p.score, p.num_comments "
        "FROM needs n JOIN posts p ON p.id = n.post_id"
    ).fetchall()
    if not rows:
        sys.exit("Aucun besoin en base : lancez d'abord collect puis analyze.")

    groups = defaultdict(list)
    for r in rows:
        groups[r["category"]].append(r)

    ranked = []
    for cat, items in groups.items():
        total = sum(need_score(x) for x in items)
        # la fréquence compte : plusieurs posts indépendants sur la même catégorie
        ranked.append((total * (1 + math.log(len(items))), cat, items))
    ranked.sort(key=lambda t: t[0], reverse=True)

    with open(REPORT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["categorie", "score_cat", "nb_posts", "need", "audience", "intensite",
                    "volonte_payer", "faisabilite", "solution_actuelle", "subreddit", "url"])
        for total, cat, items in ranked:
            for x in sorted(items, key=need_score, reverse=True):
                w.writerow([cat, round(total, 1), len(items), x["need"], x["audience"],
                            x["intensity"], x["willingness_to_pay"], x["digital_feasibility"],
                            x["current_solution"], x["subreddit"], x["url"]])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(f"# Besoins détectés sur Reddit\n\nGénéré le {now} — {len(rows)} besoins, "
                f"{len(ranked)} catégories\n\n")
        for rank, (total, cat, items) in enumerate(ranked[:30], 1):
            f.write(f"## {rank}. {cat} (score {total:.0f}, {len(items)} posts)\n\n")
            for x in sorted(items, key=need_score, reverse=True)[:5]:
                f.write(f"- **{x['need']}**  \n"
                        f"  Public : {x['audience']} | intensité {x['intensity']}/5 | "
                        f"paiement {x['willingness_to_pay']}/3 | faisabilité {x['digital_feasibility']}/5  \n"
                        f"  Aujourd'hui : {x['current_solution']}  \n"
                        f"  r/{x['subreddit']} — [voir le post]({x['url']})\n")
            f.write("\n")

    print(f"[report] {REPORT_MD}\n[report] {REPORT_CSV}")


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "collect":
        collect()
    elif cmd == "analyze":
        analyze()
    elif cmd == "report":
        report()
    elif cmd == "purge":
        purge()
    elif cmd == "all":
        collect()
        analyze()
        report()
    else:
        sys.exit("Commande inconnue : collect | analyze | report | purge | all")
