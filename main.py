"""
Google Ads MCP Server - Les Belles Combines
===========================================
Serveur MCP (Model Context Protocol) exposant le compte Google Ads d'une
marque a Claude, concu pour etre heberge sur Railway (transport HTTP
"streamable"). Base : le serveur Google Ads de Snoc (memes outils), avec les
conventions du serveur Meta Ads des Belles Combines : une cle d'acces par
personne avec un role, marque et compte decrits par les variables
d'environnement, rien en dur.

Securite :
  - Point d'entree MCP : /mcp/<secret> (ou /mcp avec un en-tete
    Authorization: Bearer <secret>), le secret etant l'une des cles de
    AUTH_KEYS ("secret:nom:role", role full ou read).
  - Une cle "read" ne voit et n'appelle que les outils de lecture ; les outils
    d'ecriture exigent une cle "full" ET GOOGLE_ADS_ALLOW_WRITES=true.
  - GOOGLE_ADS_CUSTOMER_ID limite le serveur au(x) compte(s) de la marque ; le
    premier est le compte par defaut des outils (customer_id facultatif).

Variables d'environnement requises (voir .env.example / README) :
  AUTH_KEYS, GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID,
  GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN
Optionnelles :
  GOOGLE_ADS_CUSTOMER_ID (compte(s) servi(s), 10 chiffres ; le premier = defaut)
  GOOGLE_ADS_LOGIN_CUSTOMER_ID (ID du compte administrateur/MCC, sans tirets)
  GOOGLE_ADS_ALLOW_WRITES ("true" pour autoriser les modifications)
  BRAND_NAME (nom de la marque : instructions, /health, journaux)
  MCP_ALLOWED_HOSTS (protection anti-DNS-rebinding, voir plus bas)
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
SERVICE_NAME = "lbc-google-ads-mcp"
STARTED_AT = time.monotonic()

# Nom de la marque (instructions du serveur, /health, journaux).
BRAND = os.environ.get("BRAND_NAME", "").strip()
ALLOW_WRITES = os.environ.get("GOOGLE_ADS_ALLOW_WRITES", "false").strip().lower() in (
    "true", "1", "yes", "oui",
)

# Cles d'acces, meme format que le serveur Meta Ads :
#   AUTH_KEYS = "secret:nom:role[,secret2:nom2:role2]"
# Role "full" (lecture + ecriture) ou "read" (lecture seule) ; sans role : full.
# Secret de 16 caracteres minimum, caracteres surs dans une URL uniquement
# (lettres, chiffres, - _ . ~), puisqu'il fait partie du chemin /mcp/<secret>.
AUTH_ROLE_ALIASES = {
    "": "full", "full": "full", "complet": "full", "read": "read", "lecture": "read",
}
_SECRET_RE = re.compile(r"^[A-Za-z0-9_.~-]{16,}$")


def parse_auth_keys(raw: str) -> tuple[dict[str, dict], list[str]]:
    """Retourne ({secret: {"name", "role"}}, avertissements des cles ignorees)."""
    keys: dict[str, dict] = {}
    warnings: list[str] = []
    for position, entry in enumerate((raw or "").split(","), 1):
        entry = entry.strip()
        if not entry:
            continue
        secret, _, rest = entry.partition(":")
        name, _, role = rest.partition(":")
        secret, name, role = secret.strip(), name.strip() or "?", role.strip().lower()
        if not _SECRET_RE.match(secret):
            warnings.append(
                f"cle n. {position} ({name}) ignoree : secret de moins de 16 caracteres "
                "ou avec des caracteres interdits (autorises : lettres, chiffres, - _ . ~)"
            )
            continue
        if role not in AUTH_ROLE_ALIASES:
            warnings.append(
                f"cle n. {position} ({name}) ignoree : role {role!r} inconnu (full ou read)"
            )
            continue
        keys[secret] = {"name": name, "role": AUTH_ROLE_ALIASES[role]}
    return keys, warnings


AUTH_KEYS, AUTH_KEY_WARNINGS = parse_auth_keys(os.environ.get("AUTH_KEYS", ""))

# La librairie google-ads exige ce parametre ; on le fixe par defaut.
os.environ.setdefault("GOOGLE_ADS_USE_PROTO_PLUS", "True")

# Nettoie l'ID du compte administrateur si fourni avec des tirets/espaces.
_login_cid = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
if _login_cid:
    os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = re.sub(r"[^0-9]", "", _login_cid)

# Protection anti "DNS rebinding" du SDK MCP : elle renvoie 421 a toute requete
# dont l'en-tete Host n'est pas dans la liste autorisee (localhost par defaut),
# ce qui bloque un serveur public. Ici l'acces est deja protege par AUTH_KEYS :
# on la desactive, sauf si MCP_ALLOWED_HOSTS liste explicitement les domaines
# autorises (ex. "mcp.exemple.com,xxxx.up.railway.app").
_allowed_hosts = [
    h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
]
if _allowed_hosts:
    TRANSPORT_SECURITY = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h for host in _allowed_hosts for h in (host, f"{host}:*")],
        allowed_origins=[f"https://{host}" for host in _allowed_hosts],
    )
else:
    TRANSPORT_SECURITY = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

REQUIRED_GOOGLE_VARS = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
)

# ---------------------------------------------------------------------------
# Client Google Ads (initialise paresseusement au premier appel d'outil)
# ---------------------------------------------------------------------------

_client = None


def get_client():
    """Retourne le client Google Ads, en le creant au premier appel."""
    global _client
    if _client is None:
        missing = [v for v in REQUIRED_GOOGLE_VARS if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                "Configuration Google Ads incomplete. Variables manquantes : "
                + ", ".join(missing)
            )
        from google.ads.googleads.client import GoogleAdsClient

        _client = GoogleAdsClient.load_from_env()
    return _client


def norm_cid(customer_id: str) -> str:
    """Normalise un ID client ('123-456-7890' -> '1234567890')."""
    cid = re.sub(r"[^0-9]", "", str(customer_id))
    if len(cid) != 10:
        raise ValueError(
            f"ID client invalide : {customer_id!r}. Attendu : 10 chiffres, "
            "par ex. 1234567890 ou 123-456-7890."
        )
    return cid


def parse_customer_ids(raw: str) -> tuple[list[str], list[str]]:
    """GOOGLE_ADS_CUSTOMER_ID : un ou plusieurs IDs separes par des virgules
    -> (IDs normalises, sans doublon, ordre conserve ; erreurs)."""
    ids: list[str] = []
    errors: list[str] = []
    for part in re.split(r"[,;]", raw or ""):
        part = part.strip()
        if not part:
            continue
        try:
            cid = norm_cid(part)
        except ValueError as ex:
            errors.append(str(ex))
            continue
        if cid not in ids:
            ids.append(cid)
    return ids, errors


# Compte(s) Google Ads servi(s) par ce deploiement ; le premier est le compte
# par defaut des outils. Vide = tout compte accessible (customer_id requis).
SERVED_CUSTOMER_IDS, CUSTOMER_ID_ERRORS = parse_customer_ids(
    os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
)


def resolve_cid(customer_id: Optional[str] = None) -> str:
    """ID client d'un appel d'outil : celui fourni (normalise, et limite aux
    comptes servis si GOOGLE_ADS_CUSTOMER_ID est defini), sinon le compte par
    defaut."""
    if customer_id is None or not str(customer_id).strip():
        if not SERVED_CUSTOMER_IDS:
            raise ValueError(
                "customer_id requis : aucun compte par defaut n'est configure sur ce "
                "serveur (GOOGLE_ADS_CUSTOMER_ID). Trouver l'ID avec list_accounts."
            )
        return SERVED_CUSTOMER_IDS[0]
    cid = norm_cid(customer_id)
    if SERVED_CUSTOMER_IDS and cid not in SERVED_CUSTOMER_IDS:
        raise ValueError(
            f"Le compte {cid} n'est pas servi par ce serveur. Compte(s) autorise(s) "
            f"(GOOGLE_ADS_CUSTOMER_ID) : {', '.join(SERVED_CUSTOMER_IDS)}."
        )
    return cid


def row_to_dict(row: Any) -> dict:
    """Convertit une ligne GAQL (proto) en dictionnaire lisible."""
    from google.protobuf.json_format import MessageToDict

    return MessageToDict(row._pb, preserving_proto_field_name=True)


def ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def fail(payload: Any) -> str:
    return json.dumps({"error": payload}, ensure_ascii=False, default=str)


def format_google_ads_error(ex: Exception) -> str:
    """Transforme une GoogleAdsException en message exploitable par Claude."""
    from google.ads.googleads.errors import GoogleAdsException

    if isinstance(ex, GoogleAdsException):
        details = []
        for err in ex.failure.errors:
            details.append(
                {
                    "message": err.message,
                    "code": str(err.error_code).strip(),
                    "field": ".".join(
                        p.field_name for p in err.location.field_path_elements
                    )
                    or None,
                }
            )
        return fail(
            {
                "type": "GoogleAdsException",
                "request_id": ex.request_id,
                "details": details,
                "hint": (
                    "Codes frequents : DEVELOPER_TOKEN_NOT_APPROVED = le token n'a "
                    "que l'acces test (demander l'acces Basic) ; "
                    "USER_PERMISSION_DENIED = l'utilisateur OAuth n'a pas acces a ce "
                    "compte, ou GOOGLE_ADS_LOGIN_CUSTOMER_ID est absent/incorrect ; "
                    "CUSTOMER_NOT_ENABLED = compte non active (nouveau compte sans "
                    "facturation, ou compte annule)."
                ),
            }
        )
    msg = str(ex)
    payload: dict[str, Any] = {"type": type(ex).__name__, "message": msg}
    if "invalid_grant" in msg:
        payload["hint"] = (
            "Le refresh token est expire ou revoque. Cause la plus frequente : "
            "l'ecran de consentement OAuth est en mode 'Test' (token valable 7 "
            "jours). Publier l'application 'En production' dans Google Cloud "
            "Console, regenerer un refresh token avec get_refresh_token.py et "
            "mettre a jour GOOGLE_ADS_REFRESH_TOKEN sur Railway."
        )
    return fail(payload)


def run_query(customer_id: str, query: str, limit: int = 200) -> list[dict]:
    """Execute une requete GAQL et retourne au plus `limit` lignes."""
    client = get_client()
    ga_service = client.get_service("GoogleAdsService")
    rows: list[dict] = []
    for row in ga_service.search(customer_id=customer_id, query=query):
        rows.append(row_to_dict(row))
        if len(rows) >= limit:
            break
    return rows


def date_clause(last_n_days: int) -> str:
    end = date.today()
    start = end - timedelta(days=max(1, int(last_n_days)))
    return f"segments.date BETWEEN '{start:%Y-%m-%d}' AND '{end:%Y-%m-%d}'"


def micros_to_unit(v: Any) -> Optional[float]:
    try:
        return round(int(v) / 1_000_000, 2)
    except (TypeError, ValueError):
        return None


def require_writes() -> None:
    if not ALLOW_WRITES:
        raise RuntimeError(
            "Les modifications sont desactivees sur ce serveur. "
            "Pour les activer, definir GOOGLE_ADS_ALLOW_WRITES=true dans les "
            "variables d'environnement Railway, puis redeployer."
        )


def campaign_row(cid: str, campaign_id: str, fields: str) -> Optional[dict]:
    """Retourne la ligne GAQL (dict) d'une campagne, ou None si introuvable."""
    rows = run_query(
        cid,
        f"SELECT {fields} FROM campaign WHERE campaign.id = {int(campaign_id)}",
        limit=1,
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Validation des specifications et construction des operations de mutation
# (fonctions pures, testables hors ligne : voir tests/offline_build_test.py)
# ---------------------------------------------------------------------------


class SpecError(ValueError):
    """Specification invalide fournie par l'utilisateur (message deja lisible)."""


MATCH_TYPES = ("EXACT", "PHRASE", "BROAD")
BIDDING_TYPES = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "TARGET_CPA")
RSA_HEADLINE_MAX_LEN = 30
RSA_DESCRIPTION_MAX_LEN = 90
RSA_PATH_MAX_LEN = 15
RSA_MIN_HEADLINES, RSA_MAX_HEADLINES = 3, 15
RSA_MIN_DESCRIPTIONS, RSA_MAX_DESCRIPTIONS = 2, 4
SITELINK_TEXT_MAX_LEN = 25
SITELINK_DESCRIPTION_MAX_LEN = 35
CALLOUT_MAX_LEN = 25
CALLOUTS_MAX = 20
KEYWORD_MAX_LEN = 80
SEARCH_THEMES_MAX = 25  # limite Google par groupe d'assets


def _clean_text(
    value: Any, label: str, max_len: Optional[int] = None, required: bool = True
) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        if required:
            raise SpecError(f"{label} : texte manquant.")
        return ""
    if max_len is not None and len(text) > max_len:
        raise SpecError(
            f"{label} : {len(text)} caracteres, maximum {max_len} ({text!r})."
        )
    return text


def _clean_url(value: Any, label: str) -> str:
    url = _clean_text(value, label)
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise SpecError(
            f"{label} : l'URL doit commencer par http:// ou https:// ({url!r})."
        )
    return url


def _clean_texts(
    values: Any, label: str, max_len: int, min_count: int, max_count: int
) -> list[str]:
    if values is None:
        values = []
    if not isinstance(values, (list, tuple)):
        raise SpecError(f"{label} : liste de textes attendue.")
    out: list[str] = []
    for i, value in enumerate(values, 1):
        text = _clean_text(value, f"{label}[{i}]", max_len=max_len)
        if text in out:
            raise SpecError(f"{label} : doublon {text!r}.")
        out.append(text)
    if not min_count <= len(out) <= max_count:
        raise SpecError(
            f"{label} : {len(out)} element(s), attendu entre {min_count} et "
            f"{max_count}."
        )
    return out


def _clean_int_list(values: Any, label: str) -> list[int]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise SpecError(f"{label} : liste d'entiers attendue.")
    out: list[int] = []
    for value in values:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise SpecError(f"{label} : {value!r} n'est pas un entier.")
        if n not in out:
            out.append(n)
    return out


def _clean_positive_float(value: Any, label: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SpecError(f"{label} : nombre attendu (recu {value!r}).")
    if number <= 0:
        raise SpecError(f"{label} doit etre positif.")
    return number


def _clean_keyword(item: Any, label: str) -> dict:
    if not isinstance(item, dict):
        raise SpecError(f"{label} : objet {{'text', 'match_type'}} attendu.")
    text = _clean_text(item.get("text"), f"{label}.text", max_len=KEYWORD_MAX_LEN)
    match_type = str(item.get("match_type") or "").strip().upper()
    if match_type not in MATCH_TYPES:
        raise SpecError(
            f"{label}.match_type doit etre EXACT, PHRASE ou BROAD "
            f"(recu {item.get('match_type')!r})."
        )
    return {"text": text, "match_type": match_type}


def _clean_keywords(values: Any, label: str) -> list[dict]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise SpecError(f"{label} : liste d'objets {{'text', 'match_type'}} attendue.")
    out: list[dict] = []
    for i, item in enumerate(values, 1):
        kw = _clean_keyword(item, f"{label}[{i}]")
        if kw not in out:  # doublons ignores silencieusement
            out.append(kw)
    return out


def validate_ad_group_spec(spec: Any, label: str = "spec") -> dict:
    """Valide/normalise la specification d'un groupe d'annonces Search + RSA."""
    if not isinstance(spec, dict):
        raise SpecError(f"{label} : objet attendu.")
    name = _clean_text(spec.get("name"), f"{label}.name")
    final_url = _clean_url(spec.get("final_url"), f"{label}.final_url")
    keywords = _clean_keywords(spec.get("keywords"), f"{label}.keywords")
    if not keywords:
        raise SpecError(f"{label}.keywords : fournir au moins un mot-cle.")
    headlines = _clean_texts(
        spec.get("headlines"),
        f"{label}.headlines",
        RSA_HEADLINE_MAX_LEN,
        RSA_MIN_HEADLINES,
        RSA_MAX_HEADLINES,
    )
    descriptions = _clean_texts(
        spec.get("descriptions"),
        f"{label}.descriptions",
        RSA_DESCRIPTION_MAX_LEN,
        RSA_MIN_DESCRIPTIONS,
        RSA_MAX_DESCRIPTIONS,
    )
    path1 = _clean_text(
        spec.get("path1"), f"{label}.path1", max_len=RSA_PATH_MAX_LEN, required=False
    )
    path2 = _clean_text(
        spec.get("path2"), f"{label}.path2", max_len=RSA_PATH_MAX_LEN, required=False
    )
    if path2 and not path1:
        raise SpecError(f"{label}.path2 necessite path1.")
    return {
        "name": name,
        "final_url": final_url,
        "keywords": keywords,
        "headlines": headlines,
        "descriptions": descriptions,
        "path1": path1,
        "path2": path2,
    }


def validate_sitelinks(sitelinks: Any) -> list[dict]:
    if sitelinks is None:
        return []
    if not isinstance(sitelinks, (list, tuple)):
        raise SpecError("sitelinks : liste d'objets attendue.")
    out: list[dict] = []
    for i, item in enumerate(sitelinks, 1):
        label = f"sitelinks[{i}]"
        if not isinstance(item, dict):
            raise SpecError(
                f"{label} : objet {{'text', 'final_url', 'description1', "
                "'description2'}} attendu."
            )
        text = _clean_text(
            item.get("text"), f"{label}.text", max_len=SITELINK_TEXT_MAX_LEN
        )
        final_url = _clean_url(item.get("final_url"), f"{label}.final_url")
        d1 = _clean_text(
            item.get("description1"),
            f"{label}.description1",
            max_len=SITELINK_DESCRIPTION_MAX_LEN,
            required=False,
        )
        d2 = _clean_text(
            item.get("description2"),
            f"{label}.description2",
            max_len=SITELINK_DESCRIPTION_MAX_LEN,
            required=False,
        )
        if bool(d1) != bool(d2):
            raise SpecError(
                f"{label} : description1 et description2 vont ensemble "
                "(les deux ou aucune)."
            )
        out.append(
            {
                "text": text,
                "final_url": final_url,
                "description1": d1,
                "description2": d2,
            }
        )
    return out


def validate_callouts(callouts: Any) -> list[str]:
    return _clean_texts(callouts, "callouts", CALLOUT_MAX_LEN, 0, CALLOUTS_MAX)


def validate_search_campaign_spec(spec: Any) -> dict:
    """Valide/normalise la specification complete d'une campagne Search."""
    if not isinstance(spec, dict):
        raise SpecError("spec : objet attendu.")
    name = _clean_text(spec.get("name"), "spec.name")
    daily_budget = _clean_positive_float(spec.get("daily_budget"), "spec.daily_budget")
    if daily_budget is None:
        raise SpecError(
            "spec.daily_budget : montant quotidien requis (devise du compte)."
        )
    status = str(spec.get("status") or "PAUSED").strip().upper()
    if status not in ("PAUSED", "ENABLED"):
        raise SpecError("spec.status doit etre PAUSED ou ENABLED.")

    bidding = spec.get("bidding") or {}
    if not isinstance(bidding, dict):
        raise SpecError(
            "spec.bidding : objet {'type', 'target_cpa', 'target_roas'} attendu."
        )
    bid_type = str(bidding.get("type") or "MAXIMIZE_CONVERSIONS").strip().upper()
    if bid_type not in BIDDING_TYPES:
        raise SpecError(
            "spec.bidding.type doit etre MAXIMIZE_CONVERSIONS, "
            "MAXIMIZE_CONVERSION_VALUE ou TARGET_CPA."
        )
    target_cpa = _clean_positive_float(
        bidding.get("target_cpa"), "spec.bidding.target_cpa"
    )
    target_roas = _clean_positive_float(
        bidding.get("target_roas"), "spec.bidding.target_roas"
    )
    if bid_type == "TARGET_CPA" and target_cpa is None:
        raise SpecError("spec.bidding.target_cpa est requis pour TARGET_CPA.")
    if bid_type == "MAXIMIZE_CONVERSION_VALUE" and target_cpa is not None:
        raise SpecError(
            "spec.bidding.target_cpa ne s'applique pas a MAXIMIZE_CONVERSION_VALUE "
            "(utiliser target_roas)."
        )
    if bid_type != "MAXIMIZE_CONVERSION_VALUE" and target_roas is not None:
        raise SpecError(
            "spec.bidding.target_roas ne s'applique qu'a MAXIMIZE_CONVERSION_VALUE."
        )

    geo_ids = _clean_int_list(
        spec.get("geo_target_constant_ids"), "spec.geo_target_constant_ids"
    )
    if not geo_ids:
        raise SpecError(
            "spec.geo_target_constant_ids : fournir au moins une zone geographique "
            "(ex. 20123 = Quebec, 2124 = Canada) pour eviter un ciblage mondial."
        )
    language_ids = _clean_int_list(
        spec.get("language_constant_ids"), "spec.language_constant_ids"
    )
    negatives = _clean_keywords(spec.get("negative_keywords"), "spec.negative_keywords")

    ad_groups_raw = spec.get("ad_groups")
    if not isinstance(ad_groups_raw, (list, tuple)) or not ad_groups_raw:
        raise SpecError("spec.ad_groups : fournir au moins un groupe d'annonces.")
    ad_groups = [
        validate_ad_group_spec(ag, f"spec.ad_groups[{i}]")
        for i, ag in enumerate(ad_groups_raw, 1)
    ]
    names = [ag["name"] for ag in ad_groups]
    if len(set(names)) != len(names):
        raise SpecError("spec.ad_groups : plusieurs groupes portent le meme nom.")

    return {
        "name": name,
        "daily_budget": daily_budget,
        "status": status,
        "bidding": {
            "type": bid_type,
            "target_cpa": target_cpa,
            "target_roas": target_roas,
        },
        "geo_target_constant_ids": geo_ids,
        "language_constant_ids": language_ids,
        "negative_keywords": negatives,
        "ad_groups": ad_groups,
        "sitelinks": validate_sitelinks(spec.get("sitelinks")),
        "callouts": validate_callouts(spec.get("callouts")),
    }


def _mutate_op(client, kind: str):
    """Cree un MutateOperation et retourne (operation, objet `create` de `kind`)."""
    op = client.get_type("MutateOperation")
    return op, getattr(op, kind).create


def _build_ad_group_ops(
    client, cid: str, campaign_rn: str, ad_group: dict, temp_ids
) -> list:
    """Operations : 1 groupe d'annonces Search + 1 RSA + ses mots-cles.

    `ad_group` doit avoir ete normalise par validate_ad_group_spec ;
    `temp_ids` est un iterateur d'IDs temporaires negatifs.
    """
    svc = client.get_service("GoogleAdsService")
    ops = []

    ad_group_rn = svc.ad_group_path(cid, str(next(temp_ids)))
    op, group = _mutate_op(client, "ad_group_operation")
    group.resource_name = ad_group_rn
    group.name = ad_group["name"]
    group.campaign = campaign_rn
    group.status = client.enums.AdGroupStatusEnum.ENABLED
    group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ops.append(op)

    op, ad_group_ad = _mutate_op(client, "ad_group_ad_operation")
    ad_group_ad.ad_group = ad_group_rn
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(ad_group["final_url"])
    rsa = ad_group_ad.ad.responsive_search_ad
    for text in ad_group["headlines"]:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.headlines.append(asset)
    for text in ad_group["descriptions"]:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.descriptions.append(asset)
    if ad_group["path1"]:
        rsa.path1 = ad_group["path1"]
    if ad_group["path2"]:
        rsa.path2 = ad_group["path2"]
    ops.append(op)

    for kw in ad_group["keywords"]:
        op, crit = _mutate_op(client, "ad_group_criterion_operation")
        crit.ad_group = ad_group_rn
        crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        crit.keyword.text = kw["text"]
        crit.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"]
        )
        ops.append(op)
    return ops


def _build_campaign_asset_ops(
    client,
    cid: str,
    campaign_rn: str,
    sitelinks: list[dict],
    callouts: list[str],
    temp_ids,
) -> list:
    """Operations : assets sitelink/callout + liaison CampaignAsset."""
    svc = client.get_service("GoogleAdsService")
    ops = []
    for link in sitelinks:
        asset_rn = svc.asset_path(cid, str(next(temp_ids)))
        op, asset = _mutate_op(client, "asset_operation")
        asset.resource_name = asset_rn
        asset.final_urls.append(link["final_url"])
        asset.sitelink_asset.link_text = link["text"]
        if link["description1"]:
            asset.sitelink_asset.description1 = link["description1"]
            asset.sitelink_asset.description2 = link["description2"]
        ops.append(op)

        op, campaign_asset = _mutate_op(client, "campaign_asset_operation")
        campaign_asset.campaign = campaign_rn
        campaign_asset.asset = asset_rn
        campaign_asset.field_type = client.enums.AssetFieldTypeEnum.SITELINK
        ops.append(op)

    for text in callouts:
        asset_rn = svc.asset_path(cid, str(next(temp_ids)))
        op, asset = _mutate_op(client, "asset_operation")
        asset.resource_name = asset_rn
        asset.callout_asset.callout_text = text
        ops.append(op)

        op, campaign_asset = _mutate_op(client, "campaign_asset_operation")
        campaign_asset.campaign = campaign_rn
        campaign_asset.asset = asset_rn
        campaign_asset.field_type = client.enums.AssetFieldTypeEnum.CALLOUT
        ops.append(op)
    return ops


def _build_search_campaign_ops(client, cid: str, spec: dict) -> list:
    """Toutes les operations d'une campagne Search complete (IDs temporaires).

    `spec` doit avoir ete normalisee par validate_search_campaign_spec. Ordre :
    budget, campagne, criteres de campagne (zones, langues, negatifs), groupes
    d'annonces (+ RSA + mots-cles), assets (+ liaisons).
    """
    svc = client.get_service("GoogleAdsService")
    temp_ids = itertools.count(-1, -1)
    ops = []

    budget_rn = svc.campaign_budget_path(cid, str(next(temp_ids)))
    op, budget = _mutate_op(client, "campaign_budget_operation")
    budget.resource_name = budget_rn
    budget.name = f"{spec['name']} - budget {datetime.now():%Y-%m-%d %H:%M}"
    budget.amount_micros = int(round(spec["daily_budget"] * 1_000_000))
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    ops.append(op)

    campaign_rn = svc.campaign_path(cid, str(next(temp_ids)))
    op, campaign = _mutate_op(client, "campaign_operation")
    campaign.resource_name = campaign_rn
    campaign.name = spec["name"]
    campaign.status = getattr(client.enums.CampaignStatusEnum, spec["status"])
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.campaign_budget = budget_rn
    bidding = spec["bidding"]
    if bidding["type"] == "MAXIMIZE_CONVERSION_VALUE":
        campaign.maximize_conversion_value = client.get_type("MaximizeConversionValue")
        if bidding["target_roas"]:
            campaign.maximize_conversion_value.target_roas = float(
                bidding["target_roas"]
            )
    else:
        # MAXIMIZE_CONVERSIONS, avec ou sans CPA cible. TARGET_CPA est realise
        # comme "Maximiser les conversions + CPA cible", la forme recommandee
        # par Google pour les nouvelles campagnes.
        campaign.maximize_conversions = client.get_type("MaximizeConversions")
        if bidding["target_cpa"]:
            campaign.maximize_conversions.target_cpa_micros = int(
                round(bidding["target_cpa"] * 1_000_000)
            )
    network = campaign.network_settings
    network.target_google_search = True
    network.target_search_network = False
    network.target_content_network = False
    network.target_partner_search_network = False
    geo_setting = campaign.geo_target_type_setting
    geo_setting.positive_geo_target_type = (
        client.enums.PositiveGeoTargetTypeEnum.PRESENCE
    )
    geo_setting.negative_geo_target_type = (
        client.enums.NegativeGeoTargetTypeEnum.PRESENCE
    )
    campaign.contains_eu_political_advertising = getattr(
        client.enums.EuPoliticalAdvertisingStatusEnum,
        "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    )
    ops.append(op)

    for geo_id in spec["geo_target_constant_ids"]:
        op, crit = _mutate_op(client, "campaign_criterion_operation")
        crit.campaign = campaign_rn
        crit.location.geo_target_constant = svc.geo_target_constant_path(str(geo_id))
        ops.append(op)
    for language_id in spec["language_constant_ids"]:
        op, crit = _mutate_op(client, "campaign_criterion_operation")
        crit.campaign = campaign_rn
        crit.language.language_constant = svc.language_constant_path(str(language_id))
        ops.append(op)
    for kw in spec["negative_keywords"]:
        op, crit = _mutate_op(client, "campaign_criterion_operation")
        crit.campaign = campaign_rn
        crit.negative = True
        crit.keyword.text = kw["text"]
        crit.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"]
        )
        ops.append(op)

    for ad_group in spec["ad_groups"]:
        ops.extend(_build_ad_group_ops(client, cid, campaign_rn, ad_group, temp_ids))
    ops.extend(
        _build_campaign_asset_ops(
            client, cid, campaign_rn, spec["sitelinks"], spec["callouts"], temp_ids
        )
    )
    return ops


# ---------------------------------------------------------------------------
# Performance Max : validation de la specification, lecture d'une campagne
# source (clonage) et construction des operations (fonctions pures)
# ---------------------------------------------------------------------------

PMAX_HEADLINE_MAX_LEN = 30
PMAX_LONG_HEADLINE_MAX_LEN = 90
PMAX_DESCRIPTION_MAX_LEN = 90
PMAX_SHORT_DESCRIPTION_MAX_LEN = 60  # au moins une description <= 60
PMAX_MIN_HEADLINES, PMAX_MAX_HEADLINES = 3, 15
PMAX_MIN_LONG_HEADLINES, PMAX_MAX_LONG_HEADLINES = 1, 5
PMAX_MIN_DESCRIPTIONS, PMAX_MAX_DESCRIPTIONS = 2, 5
PMAX_BUSINESS_NAME_MAX_LEN = 25
PMAX_MAX_IMAGES_PER_TYPE = 20
PMAX_MAX_VIDEOS = 5
PMAX_LOGO_MIN_PX = 128  # logos carres : 128 x 128 minimum
PMAX_LANDSCAPE_LOGO_MIN = (512, 128)
PMAX_GEO_TARGET_TYPES = ("PRESENCE", "PRESENCE_OR_INTEREST")
# Types d'assets repris d'une campagne source lors d'un clonage.
PMAX_CLONED_GROUP_FIELD_TYPES = (
    "MARKETING_IMAGE",
    "SQUARE_MARKETING_IMAGE",
    "PORTRAIT_MARKETING_IMAGE",
    "YOUTUBE_VIDEO",
    "BUSINESS_NAME",
    "LOGO",
    "LANDSCAPE_LOGO",
)
PMAX_CLONED_CAMPAIGN_FIELD_TYPES = ("BUSINESS_NAME", "LOGO", "LANDSCAPE_LOGO")
PMAX_ASSET_KEYS = {
    "MARKETING_IMAGE": "marketing_image_asset_ids",
    "SQUARE_MARKETING_IMAGE": "square_marketing_image_asset_ids",
    "PORTRAIT_MARKETING_IMAGE": "portrait_marketing_image_asset_ids",
    "YOUTUBE_VIDEO": "youtube_video_asset_ids",
    "LOGO": "logo_asset_ids",
    "LANDSCAPE_LOGO": "landscape_logo_asset_ids",
}


def _clean_id_list(values: Any, label: str, max_count: Optional[int] = None) -> list[str]:
    """Liste d'IDs numeriques (str), doublons retires, ordre conserve."""
    ids = [str(n) for n in _clean_int_list(values, label)]
    if any(int(i) <= 0 for i in ids):
        raise SpecError(f"{label} : IDs positifs attendus.")
    if max_count is not None and len(ids) > max_count:
        raise SpecError(f"{label} : {len(ids)} elements, maximum {max_count}.")
    return ids


def _clean_item_ids(values: Any, label: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise SpecError(f"{label} : liste d'identifiants produit attendue.")
    out: list[str] = []
    for i, value in enumerate(values, 1):
        text = _clean_text(value, f"{label}[{i}]", max_len=120)
        if text not in out:
            out.append(text)
    return out


def validate_pmax_campaign_spec(spec: Any) -> dict:
    """Valide/normalise la specification d'une campagne Performance Max.

    Les assets images/videos/logos sont des IDs d'assets EXISTANTS du compte
    (reutilises) ; les textes sont crees. Avec `clone_from_campaign_id`, les
    champs absents (marchand, images, videos, logos, nom d'entreprise, fiches
    produits, audiences) sont repris de la campagne source par
    resolve_pmax_clone_source() ; les verifications de presence des assets
    obligatoires se font apres cette fusion (finalize_pmax_spec).
    """
    if not isinstance(spec, dict):
        raise SpecError("spec : objet attendu.")
    name = _clean_text(spec.get("name"), "spec.name")
    daily_budget = _clean_positive_float(spec.get("daily_budget"), "spec.daily_budget")
    if daily_budget is None:
        raise SpecError("spec.daily_budget : montant quotidien requis (devise du compte).")
    status = str(spec.get("status") or "PAUSED").strip().upper()
    if status not in ("PAUSED", "ENABLED"):
        raise SpecError("spec.status doit etre PAUSED ou ENABLED.")
    target_roas = _clean_positive_float(spec.get("target_roas"), "spec.target_roas")
    if target_roas is not None and target_roas >= 100:
        raise SpecError(
            "spec.target_roas : ratio attendu (4.0 = 400 %), pas un pourcentage."
        )
    geo_ids = _clean_int_list(
        spec.get("geo_target_constant_ids"), "spec.geo_target_constant_ids"
    )
    if not geo_ids:
        raise SpecError(
            "spec.geo_target_constant_ids : fournir au moins une zone geographique "
            "(ex. 20123 = Quebec, 2124 = Canada) pour eviter un ciblage mondial."
        )
    geo_type = str(spec.get("geo_target_type") or "PRESENCE").strip().upper()
    if geo_type not in PMAX_GEO_TARGET_TYPES:
        raise SpecError("spec.geo_target_type doit etre PRESENCE ou PRESENCE_OR_INTEREST.")
    language_ids = _clean_int_list(
        spec.get("language_constant_ids"), "spec.language_constant_ids"
    )
    negatives = _clean_keywords(spec.get("negative_keywords"), "spec.negative_keywords")

    final_url = _clean_url(spec.get("final_url"), "spec.final_url")
    asset_group_name = _clean_text(
        spec.get("asset_group_name") or "Groupe de composants 1", "spec.asset_group_name"
    )
    path1 = _clean_text(spec.get("path1"), "spec.path1", max_len=RSA_PATH_MAX_LEN, required=False)
    path2 = _clean_text(spec.get("path2"), "spec.path2", max_len=RSA_PATH_MAX_LEN, required=False)
    if path2 and not path1:
        raise SpecError("spec.path2 necessite path1.")
    headlines = _clean_texts(
        spec.get("headlines"), "spec.headlines", PMAX_HEADLINE_MAX_LEN,
        PMAX_MIN_HEADLINES, PMAX_MAX_HEADLINES,
    )
    long_headlines = _clean_texts(
        spec.get("long_headlines"), "spec.long_headlines", PMAX_LONG_HEADLINE_MAX_LEN,
        PMAX_MIN_LONG_HEADLINES, PMAX_MAX_LONG_HEADLINES,
    )
    descriptions = _clean_texts(
        spec.get("descriptions"), "spec.descriptions", PMAX_DESCRIPTION_MAX_LEN,
        PMAX_MIN_DESCRIPTIONS, PMAX_MAX_DESCRIPTIONS,
    )
    if not any(len(d) <= PMAX_SHORT_DESCRIPTION_MAX_LEN for d in descriptions):
        raise SpecError(
            "spec.descriptions : au moins une description de "
            f"{PMAX_SHORT_DESCRIPTION_MAX_LEN} caracteres ou moins est requise."
        )
    business_name = _clean_text(
        spec.get("business_name"), "spec.business_name",
        max_len=PMAX_BUSINESS_NAME_MAX_LEN, required=False,
    )
    business_name_asset_id = spec.get("business_name_asset_id")
    if business_name_asset_id not in (None, ""):
        business_name_asset_id = _clean_id_list(
            [business_name_asset_id], "spec.business_name_asset_id"
        )[0]
    else:
        business_name_asset_id = None
    if business_name and business_name_asset_id:
        raise SpecError(
            "spec.business_name et spec.business_name_asset_id sont exclusifs."
        )

    assets: dict[str, list[str]] = {}
    for field_type, key in PMAX_ASSET_KEYS.items():
        max_count = PMAX_MAX_VIDEOS if field_type == "YOUTUBE_VIDEO" else PMAX_MAX_IMAGES_PER_TYPE
        assets[field_type] = _clean_id_list(spec.get(key), f"spec.{key}", max_count)

    merchant_id = spec.get("merchant_id")
    merchant_id = (
        None if merchant_id in (None, "") else _clean_id_list([merchant_id], "spec.merchant_id")[0]
    )
    feed_label = _clean_text(spec.get("feed_label"), "spec.feed_label", max_len=20, required=False)
    enable_local = spec.get("enable_local")
    if enable_local is not None and not isinstance(enable_local, bool):
        raise SpecError("spec.enable_local : booleen attendu.")

    listing = spec.get("listing_group")
    include_item_ids: Optional[list[str]] = None
    if listing is not None:
        if not isinstance(listing, dict):
            raise SpecError("spec.listing_group : objet {'include_item_ids': [...]} attendu.")
        include_item_ids = _clean_item_ids(
            listing.get("include_item_ids"), "spec.listing_group.include_item_ids"
        )
        if not include_item_ids:
            raise SpecError(
                "spec.listing_group.include_item_ids : fournir au moins un identifiant "
                "produit, ou omettre listing_group pour diffuser tout le flux."
            )

    search_themes = _clean_texts(
        spec.get("search_themes"), "spec.search_themes", 80, 0, SEARCH_THEMES_MAX
    )
    audience_ids = _clean_id_list(spec.get("audience_ids"), "spec.audience_ids")
    url_expansion_opt_out = spec.get("url_expansion_opt_out", True)
    if not isinstance(url_expansion_opt_out, bool):
        raise SpecError("spec.url_expansion_opt_out : booleen attendu.")
    clone_from = spec.get("clone_from_campaign_id")
    clone_from = (
        None if clone_from in (None, "") else _clean_id_list([clone_from], "spec.clone_from_campaign_id")[0]
    )

    return {
        "name": name,
        "daily_budget": daily_budget,
        "status": status,
        "target_roas": target_roas,
        "geo_target_constant_ids": geo_ids,
        "geo_target_type": geo_type,
        "language_constant_ids": language_ids,
        "negative_keywords": negatives,
        "final_url": final_url,
        "asset_group_name": asset_group_name,
        "path1": path1,
        "path2": path2,
        "headlines": headlines,
        "long_headlines": long_headlines,
        "descriptions": descriptions,
        "business_name": business_name or None,
        "business_name_asset_id": business_name_asset_id,
        "assets": assets,
        "merchant_id": merchant_id,
        "feed_label": feed_label or None,
        "enable_local": enable_local,
        "include_item_ids": include_item_ids,
        "search_themes": search_themes,
        "audience_ids": audience_ids,
        "url_expansion_opt_out": url_expansion_opt_out,
        "sitelinks": validate_sitelinks(spec.get("sitelinks")),
        "callouts": validate_callouts(spec.get("callouts")),
        "clone_from_campaign_id": clone_from,
    }


def _asset_id_from_rn(resource_name: Any) -> Optional[str]:
    text = str(resource_name or "")
    return text.rsplit("/", 1)[-1] if "/" in text else None


def _logo_size_ok(field_type: str, asset: dict) -> bool:
    """Ecarte les logos trop petits pour Google (ex. favicon 32 x 32)."""
    full = (asset.get("image_asset") or {}).get("full_size") or {}
    try:
        w, h = int(full.get("width_pixels") or 0), int(full.get("height_pixels") or 0)
    except (TypeError, ValueError):
        return True  # dimensions inconnues : on laisse l'API trancher
    if not w or not h:
        return True
    if field_type == "LOGO":
        return w >= PMAX_LOGO_MIN_PX and h >= PMAX_LOGO_MIN_PX
    if field_type == "LANDSCAPE_LOGO":
        return w >= PMAX_LANDSCAPE_LOGO_MIN[0] and h >= PMAX_LANDSCAPE_LOGO_MIN[1]
    return True


def resolve_pmax_clone_source(cid: str, campaign_id: str) -> dict:
    """Lit, dans une campagne Performance Max existante, ce qui se reutilise.

    Retourne : reglages marchand, type de ciblage geo, nom d'entreprise et
    logos (assets de campagne ou du premier groupe d'assets), images et videos
    du premier groupe d'assets actif (source ADVERTISER seulement), fiches
    produits incluses (filtre de groupe de fiches) et audiences signalees.
    """
    camp_id = str(int(campaign_id))
    rows = run_query(
        cid,
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
        "campaign.shopping_setting.merchant_id, campaign.shopping_setting.feed_label, "
        "campaign.shopping_setting.enable_local, campaign.brand_guidelines_enabled, "
        "campaign.geo_target_type_setting.positive_geo_target_type "
        f"FROM campaign WHERE campaign.id = {camp_id}",
        limit=1,
    )
    if not rows:
        raise SpecError(f"Campagne source {campaign_id} introuvable dans le compte {cid}.")
    c = rows[0].get("campaign", {})
    if c.get("advertising_channel_type") != "PERFORMANCE_MAX":
        raise SpecError(
            f"La campagne source {c.get('name')!r} n'est pas une campagne "
            f"Performance Max ({c.get('advertising_channel_type')})."
        )
    shopping = c.get("shopping_setting") or {}
    source: dict[str, Any] = {
        "campaign_id": str(c.get("id")),
        "campaign_name": c.get("name"),
        "merchant_id": str(shopping["merchant_id"]) if shopping.get("merchant_id") else None,
        "feed_label": shopping.get("feed_label") or None,
        "enable_local": shopping.get("enable_local"),
        "brand_guidelines_enabled": bool(c.get("brand_guidelines_enabled")),
        "geo_target_type": (c.get("geo_target_type_setting") or {}).get(
            "positive_geo_target_type"
        ),
        "business_name_asset_id": None,
        "business_name": None,
        "assets": {ft: [] for ft in PMAX_ASSET_KEYS},
        "asset_group_id": None,
        "asset_group_name": None,
        "final_urls": [],
        "include_item_ids": None,
        "audience_ids": [],
    }

    def _take(field_type: str, asset: dict) -> None:
        asset_id = _asset_id_from_rn(asset.get("resource_name"))
        if not asset_id:
            return
        if field_type == "BUSINESS_NAME":
            if source["business_name_asset_id"] is None:
                source["business_name_asset_id"] = asset_id
                source["business_name"] = (asset.get("text_asset") or {}).get("text")
            return
        if field_type in ("LOGO", "LANDSCAPE_LOGO") and not _logo_size_ok(field_type, asset):
            return
        bucket = source["assets"].get(field_type)
        if bucket is not None and asset_id not in bucket:
            bucket.append(asset_id)

    # Assets de campagne (nom d'entreprise + logos, campagnes avec brand guidelines).
    for r in run_query(
        cid,
        "SELECT campaign_asset.field_type, campaign_asset.status, asset.resource_name, "
        "asset.text_asset.text, asset.image_asset.full_size.width_pixels, "
        "asset.image_asset.full_size.height_pixels FROM campaign_asset "
        f"WHERE campaign.id = {camp_id} AND campaign_asset.status = 'ENABLED'",
        limit=200,
    ):
        field_type = (r.get("campaign_asset") or {}).get("field_type")
        if field_type in PMAX_CLONED_CAMPAIGN_FIELD_TYPES:
            _take(field_type, r.get("asset") or {})

    # Premier groupe d'assets actif (sinon le premier).
    groups = run_query(
        cid,
        "SELECT asset_group.id, asset_group.name, asset_group.status, "
        f"asset_group.final_urls FROM asset_group WHERE campaign.id = {camp_id} "
        "ORDER BY asset_group.id",
        limit=50,
    )
    if not groups:
        raise SpecError(
            f"La campagne source {c.get('name')!r} n'a aucun groupe d'assets."
        )
    chosen = next(
        (g for g in groups if (g.get("asset_group") or {}).get("status") == "ENABLED"),
        groups[0],
    ).get("asset_group") or {}
    group_id = str(chosen.get("id"))
    source["asset_group_id"] = group_id
    source["asset_group_name"] = chosen.get("name")
    source["final_urls"] = list(chosen.get("final_urls") or [])

    for r in run_query(
        cid,
        "SELECT asset_group_asset.field_type, asset_group_asset.status, "
        "asset_group_asset.source, asset.resource_name, asset.text_asset.text, "
        "asset.image_asset.full_size.width_pixels, "
        "asset.image_asset.full_size.height_pixels FROM asset_group_asset "
        f"WHERE asset_group.id = {group_id} AND asset_group_asset.status = 'ENABLED'",
        limit=500,
    ):
        link = r.get("asset_group_asset") or {}
        if link.get("source") not in (None, "ADVERTISER"):
            continue  # assets generes automatiquement par Google : non repris
        field_type = link.get("field_type")
        if field_type in PMAX_CLONED_GROUP_FIELD_TYPES:
            _take(field_type, r.get("asset") or {})

    # Filtre de groupe de fiches : fiches incluses par identifiant produit.
    filters = run_query(
        cid,
        "SELECT asset_group_listing_group_filter.id, "
        "asset_group_listing_group_filter.type, "
        "asset_group_listing_group_filter.case_value.product_item_id.value "
        f"FROM asset_group_listing_group_filter WHERE asset_group.id = {group_id}",
        limit=1000,
    )
    item_ids: list[str] = []
    other_dimension = False
    for r in filters:
        f = r.get("asset_group_listing_group_filter") or {}
        if f.get("type") != "UNIT_INCLUDED":
            continue
        case_value = f.get("case_value") or {}
        value = (case_value.get("product_item_id") or {}).get("value")
        if value:
            if value not in item_ids:
                item_ids.append(value)
        elif case_value and "product_item_id" not in case_value:
            other_dimension = True
    if other_dimension and not item_ids:
        raise SpecError(
            "Le filtre de fiches de la campagne source n'est pas base sur des "
            "identifiants produit : fournir spec.listing_group.include_item_ids."
        )
    source["include_item_ids"] = item_ids or None  # None = tout le flux

    for r in run_query(
        cid,
        "SELECT asset_group_signal.audience.audience FROM asset_group_signal "
        f"WHERE asset_group.id = {group_id}",
        limit=200,
    ):
        rn = ((r.get("asset_group_signal") or {}).get("audience") or {}).get("audience")
        audience_id = _asset_id_from_rn(rn)
        if audience_id and audience_id not in source["audience_ids"]:
            source["audience_ids"].append(audience_id)
    return source


def finalize_pmax_spec(spec: dict, source: Optional[dict]) -> dict:
    """Fusionne la spec normalisee avec la campagne source, puis verifie que
    tout ce que Google exige est present."""
    merged = dict(spec)
    merged["assets"] = {ft: list(ids) for ft, ids in spec["assets"].items()}
    merged["cloned"] = {}
    if source:
        for key in ("merchant_id", "feed_label", "enable_local"):
            if merged.get(key) is None and source.get(key) is not None:
                merged[key] = source[key]
                merged["cloned"][key] = source[key]
        if not merged["business_name"] and not merged["business_name_asset_id"]:
            if source.get("business_name_asset_id"):
                merged["business_name_asset_id"] = source["business_name_asset_id"]
                merged["cloned"]["business_name"] = source.get("business_name")
        for field_type, ids in source["assets"].items():
            if not merged["assets"][field_type] and ids:
                merged["assets"][field_type] = list(ids)
                merged["cloned"][PMAX_ASSET_KEYS[field_type]] = len(ids)
        if merged["include_item_ids"] is None and source.get("include_item_ids"):
            merged["include_item_ids"] = list(source["include_item_ids"])
            merged["cloned"]["include_item_ids"] = len(source["include_item_ids"])
        if not merged["audience_ids"] and source.get("audience_ids"):
            merged["audience_ids"] = list(source["audience_ids"])
            merged["cloned"]["audience_ids"] = list(source["audience_ids"])
        merged["source_campaign"] = {
            "id": source["campaign_id"],
            "name": source["campaign_name"],
            "asset_group_id": source["asset_group_id"],
        }
    if merged["enable_local"] is None:
        merged["enable_local"] = False

    missing = []
    if not merged["assets"]["MARKETING_IMAGE"]:
        missing.append("marketing_image_asset_ids (au moins 1 image 1.91:1)")
    if not merged["assets"]["SQUARE_MARKETING_IMAGE"]:
        missing.append("square_marketing_image_asset_ids (au moins 1 image carree)")
    if not merged["assets"]["LOGO"]:
        missing.append("logo_asset_ids (au moins 1 logo carre >= 128 px)")
    if not merged["business_name"] and not merged["business_name_asset_id"]:
        missing.append("business_name (ou business_name_asset_id)")
    if merged["include_item_ids"] is not None and not merged["merchant_id"]:
        missing.append("merchant_id (requis pour filtrer des fiches produits)")
    if missing:
        raise SpecError(
            "Elements obligatoires manquants pour une campagne Performance Max : "
            + " ; ".join(missing)
            + ". Fournir ces champs ou clone_from_campaign_id."
        )
    return merged


def _build_pmax_campaign_ops(client, cid: str, spec: dict) -> list:
    """Toutes les operations d'une campagne Performance Max (IDs temporaires).

    `spec` : resultat de finalize_pmax_spec(). Ordre : budget, campagne,
    assets de campagne (nom d'entreprise, logos), criteres (zones, langues,
    negatifs), groupe d'assets, assets texte (+ liaisons), liaisons des
    images/videos existantes, filtre de groupe de fiches (racine puis
    feuilles), signaux (themes de recherche, audiences), sitelinks/callouts.
    """
    svc = client.get_service("GoogleAdsService")
    enums = client.enums
    temp_ids = itertools.count(-1, -1)
    ops = []

    budget_rn = svc.campaign_budget_path(cid, str(next(temp_ids)))
    op, budget = _mutate_op(client, "campaign_budget_operation")
    budget.resource_name = budget_rn
    budget.name = f"{spec['name']} - budget {datetime.now():%Y-%m-%d %H:%M}"
    budget.amount_micros = int(round(spec["daily_budget"] * 1_000_000))
    budget.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    ops.append(op)

    campaign_rn = svc.campaign_path(cid, str(next(temp_ids)))
    op, campaign = _mutate_op(client, "campaign_operation")
    campaign.resource_name = campaign_rn
    campaign.name = spec["name"]
    campaign.status = getattr(enums.CampaignStatusEnum, spec["status"])
    campaign.advertising_channel_type = enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    campaign.campaign_budget = budget_rn
    campaign.maximize_conversion_value = client.get_type("MaximizeConversionValue")
    if spec["target_roas"]:
        campaign.maximize_conversion_value.target_roas = float(spec["target_roas"])
    if spec["merchant_id"]:
        campaign.shopping_setting.merchant_id = int(spec["merchant_id"])
        if spec["feed_label"]:
            campaign.shopping_setting.feed_label = spec["feed_label"]
        # enable_local=False explicite est refuse a la creation
        # (OPERATION_NOT_PERMITTED_FOR_CONTEXT) : on ne l'envoie que si True.
        if spec["enable_local"]:
            campaign.shopping_setting.enable_local = True
    geo_setting = campaign.geo_target_type_setting
    geo_setting.positive_geo_target_type = getattr(
        enums.PositiveGeoTargetTypeEnum, spec["geo_target_type"]
    )
    geo_setting.negative_geo_target_type = enums.NegativeGeoTargetTypeEnum.PRESENCE
    campaign.contains_eu_political_advertising = getattr(
        enums.EuPoliticalAdvertisingStatusEnum,
        "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    )
    # Nom d'entreprise et logos portes par la campagne (brand guidelines).
    campaign.brand_guidelines_enabled = True
    if spec["url_expansion_opt_out"]:
        setting = client.get_type("Campaign").AssetAutomationSetting()
        setting.asset_automation_type = getattr(
            enums.AssetAutomationTypeEnum, FINAL_URL_EXPANSION_SETTING
        )
        setting.asset_automation_status = enums.AssetAutomationStatusEnum.OPTED_OUT
        campaign.asset_automation_settings.append(setting)
    ops.append(op)

    def link_campaign_asset(asset_rn: str, field_type: str) -> None:
        op, campaign_asset = _mutate_op(client, "campaign_asset_operation")
        campaign_asset.campaign = campaign_rn
        campaign_asset.asset = asset_rn
        campaign_asset.field_type = getattr(enums.AssetFieldTypeEnum, field_type)
        ops.append(op)

    if spec["business_name_asset_id"]:
        business_rn = svc.asset_path(cid, spec["business_name_asset_id"])
    else:
        business_rn = svc.asset_path(cid, str(next(temp_ids)))
        op, asset = _mutate_op(client, "asset_operation")
        asset.resource_name = business_rn
        asset.text_asset.text = spec["business_name"]
        ops.append(op)
    link_campaign_asset(business_rn, "BUSINESS_NAME")
    for field_type in ("LOGO", "LANDSCAPE_LOGO"):
        for asset_id in spec["assets"][field_type]:
            link_campaign_asset(svc.asset_path(cid, asset_id), field_type)

    for geo_id in spec["geo_target_constant_ids"]:
        op, crit = _mutate_op(client, "campaign_criterion_operation")
        crit.campaign = campaign_rn
        crit.location.geo_target_constant = svc.geo_target_constant_path(str(geo_id))
        ops.append(op)
    for language_id in spec["language_constant_ids"]:
        op, crit = _mutate_op(client, "campaign_criterion_operation")
        crit.campaign = campaign_rn
        crit.language.language_constant = svc.language_constant_path(str(language_id))
        ops.append(op)
    for kw in spec["negative_keywords"]:
        op, crit = _mutate_op(client, "campaign_criterion_operation")
        crit.campaign = campaign_rn
        crit.negative = True
        crit.keyword.text = kw["text"]
        crit.keyword.match_type = getattr(enums.KeywordMatchTypeEnum, kw["match_type"])
        ops.append(op)

    # Assets texte du groupe : crees AVANT le groupe d'assets, pour que les
    # liaisons AssetGroupAsset forment ensuite un bloc contigu juste apres
    # l'operation du groupe (Google valide le minimum d'assets d'un groupe sur
    # ce bloc : NOT_ENOUGH_HEADLINE_ASSET sinon).
    text_links: list[tuple[str, str]] = []
    for field_type, texts in (
        ("HEADLINE", spec["headlines"]),
        ("LONG_HEADLINE", spec["long_headlines"]),
        ("DESCRIPTION", spec["descriptions"]),
    ):
        for text in texts:
            asset_rn = svc.asset_path(cid, str(next(temp_ids)))
            op, asset = _mutate_op(client, "asset_operation")
            asset.resource_name = asset_rn
            asset.text_asset.text = text
            ops.append(op)
            text_links.append((asset_rn, field_type))

    group_temp_id = str(next(temp_ids))
    asset_group_rn = svc.asset_group_path(cid, group_temp_id)
    op, group = _mutate_op(client, "asset_group_operation")
    group.resource_name = asset_group_rn
    group.name = spec["asset_group_name"]
    group.campaign = campaign_rn
    group.final_urls.append(spec["final_url"])
    # Cree en PAUSE, puis active dans une seconde requete une fois tous ses
    # assets en place (_build_asset_group_status_op).
    group.status = enums.AssetGroupStatusEnum.PAUSED
    if spec["path1"]:
        group.path1 = spec["path1"]
    if spec["path2"]:
        group.path2 = spec["path2"]
    ops.append(op)

    def link_group_asset(asset_rn: str, field_type: str) -> None:
        op, group_asset = _mutate_op(client, "asset_group_asset_operation")
        group_asset.asset_group = asset_group_rn
        group_asset.asset = asset_rn
        group_asset.field_type = getattr(enums.AssetFieldTypeEnum, field_type)
        ops.append(op)

    for asset_rn, field_type in text_links:
        link_group_asset(asset_rn, field_type)
    for field_type in (
        "MARKETING_IMAGE",
        "SQUARE_MARKETING_IMAGE",
        "PORTRAIT_MARKETING_IMAGE",
        "YOUTUBE_VIDEO",
    ):
        for asset_id in spec["assets"][field_type]:
            link_group_asset(svc.asset_path(cid, asset_id), field_type)

    # Filtre de groupe de fiches (listing group filter).
    def filter_rn() -> str:
        return svc.asset_group_listing_group_filter_path(
            cid, group_temp_id, str(next(temp_ids))
        )

    if spec["include_item_ids"] is None:
        op, node = _mutate_op(client, "asset_group_listing_group_filter_operation")
        node.resource_name = filter_rn()
        node.asset_group = asset_group_rn
        node.type_ = enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
        node.listing_source = enums.ListingGroupFilterListingSourceEnum.SHOPPING
        ops.append(op)
    else:
        root_rn = filter_rn()
        op, root = _mutate_op(client, "asset_group_listing_group_filter_operation")
        root.resource_name = root_rn
        root.asset_group = asset_group_rn
        root.type_ = enums.ListingGroupFilterTypeEnum.SUBDIVISION
        root.listing_source = enums.ListingGroupFilterListingSourceEnum.SHOPPING
        ops.append(op)
        for item_id in spec["include_item_ids"]:
            op, node = _mutate_op(client, "asset_group_listing_group_filter_operation")
            node.resource_name = filter_rn()
            node.asset_group = asset_group_rn
            node.parent_listing_group_filter = root_rn
            node.type_ = enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
            node.listing_source = enums.ListingGroupFilterListingSourceEnum.SHOPPING
            node.case_value.product_item_id.value = item_id
            ops.append(op)
        # Noeud "tout le reste" : exclu (case_value.product_item_id vide).
        op, other = _mutate_op(client, "asset_group_listing_group_filter_operation")
        other.resource_name = filter_rn()
        other.asset_group = asset_group_rn
        other.parent_listing_group_filter = root_rn
        other.type_ = enums.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
        other.listing_source = enums.ListingGroupFilterListingSourceEnum.SHOPPING
        other.case_value.product_item_id = client.get_type(
            "ListingGroupFilterDimension"
        ).ProductItemId()
        ops.append(op)

    for theme in spec["search_themes"]:
        op, signal = _mutate_op(client, "asset_group_signal_operation")
        signal.asset_group = asset_group_rn
        signal.search_theme.text = theme
        ops.append(op)
    for audience_id in spec["audience_ids"]:
        op, signal = _mutate_op(client, "asset_group_signal_operation")
        signal.asset_group = asset_group_rn
        signal.audience.audience = svc.audience_path(cid, audience_id)
        ops.append(op)

    ops.extend(
        _build_campaign_asset_ops(
            client, cid, campaign_rn, spec["sitelinks"], spec["callouts"], temp_ids
        )
    )
    return ops


def _build_asset_group_status_op(client, asset_group_rn: str, status: str):
    """AssetGroupOperation (update) qui change le statut d'un groupe d'assets."""
    op = client.get_type("AssetGroupOperation")
    group = op.update
    group.resource_name = asset_group_rn
    group.status = getattr(client.enums.AssetGroupStatusEnum, status)
    op.update_mask.paths.append("status")
    return op


def _build_search_theme_ops(
    client, cid: str, asset_group_id: str, themes: list[str]
) -> list:
    """Operations AssetGroupSignal (search_theme) pour un groupe d'assets."""
    svc = client.get_service("AssetGroupSignalService")
    asset_group_rn = svc.asset_group_path(cid, str(int(asset_group_id)))
    ops = []
    for theme in themes:
        op = client.get_type("AssetGroupSignalOperation")
        op.create.asset_group = asset_group_rn
        op.create.search_theme.text = theme
        ops.append(op)
    return ops


def _build_target_roas_op(
    client, cid: str, campaign_id: str, target_roas: Optional[float]
):
    """CampaignOperation qui fixe le ROAS cible (ratio) ou l'efface si None."""
    from google.api_core import protobuf_helpers

    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    campaign = op.update
    campaign.resource_name = svc.campaign_path(cid, str(int(campaign_id)))
    if target_roas is None:
        # Effacement : le champ garde sa valeur par defaut (0) et figure
        # explicitement dans le masque de mise a jour.
        campaign.maximize_conversion_value.target_roas = 0.0
        op.update_mask.paths.append("maximize_conversion_value.target_roas")
    else:
        campaign.maximize_conversion_value.target_roas = float(target_roas)
        client.copy_from(
            op.update_mask, protobuf_helpers.field_mask(None, campaign._pb)
        )
    return op


def _build_goal_config_op(client, cid: str, campaign_id: str):
    """ConversionGoalCampaignConfigOperation : passe la campagne au niveau CAMPAIGN."""
    from google.api_core import protobuf_helpers

    svc = client.get_service("ConversionGoalCampaignConfigService")
    op = client.get_type("ConversionGoalCampaignConfigOperation")
    config = op.update
    config.resource_name = svc.conversion_goal_campaign_config_path(
        cid, str(int(campaign_id))
    )
    config.goal_config_level = client.enums.GoalConfigLevelEnum.CAMPAIGN
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, config._pb))
    return op


def _build_conversion_goal_ops(
    client,
    cid: str,
    campaign_id: str,
    goals: list[dict],
    biddable_categories: list[str],
) -> tuple[list, list[dict]]:
    """CampaignConversionGoalOperation pour chaque objectif dont biddable change.

    `goals` : dicts {category, origin, biddable} (etat actuel). Retourne
    (operations, changements appliques).
    """
    svc = client.get_service("CampaignConversionGoalService")
    ops, changes = [], []
    for g in goals:
        desired = g["category"] in biddable_categories
        if desired == bool(g["biddable"]):
            continue
        op = client.get_type("CampaignConversionGoalOperation")
        goal = op.update
        goal.resource_name = svc.campaign_conversion_goal_path(
            cid, str(int(campaign_id)), g["category"], g["origin"]
        )
        goal.biddable = desired
        # biddable=False est la valeur par defaut du proto : le masque doit
        # etre explicite pour que l'API applique bien la valeur.
        op.update_mask.paths.append("biddable")
        ops.append(op)
        changes.append(
            {"category": g["category"], "origin": g["origin"], "biddable": desired}
        )
    return ops, changes


FINAL_URL_EXPANSION_SETTING = "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"


def merge_automation_settings(
    current: list[dict], setting_type: str, status: str
) -> tuple[list[tuple[str, str]], Optional[str]]:
    """Recalcule la liste complete des asset_automation_settings d'une campagne.

    `current` : liste GAQL de {asset_automation_type, asset_automation_status}.
    Retourne (nouvelle liste [(type, statut)], ancien statut de `setting_type`).
    """
    previous = None
    settings: list[tuple[str, str]] = []
    for s in current or []:
        s_type = s.get("asset_automation_type")
        s_status = s.get("asset_automation_status")
        if s_type == setting_type:
            previous = s_status
            continue
        if s_type in (None, "UNSPECIFIED", "UNKNOWN") or s_status not in (
            "OPTED_IN",
            "OPTED_OUT",
        ):
            continue  # non representable dans cette version de l'API
        settings.append((s_type, s_status))
    settings.append((setting_type, status))
    return settings, previous


def _build_asset_automation_op(
    client, cid: str, campaign_id: str, settings: list[tuple[str, str]]
):
    """CampaignOperation qui remplace la liste des asset_automation_settings."""
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    campaign = op.update
    campaign.resource_name = svc.campaign_path(cid, str(int(campaign_id)))
    setting_type = client.get_type("Campaign").AssetAutomationSetting
    for s_type, s_status in settings:
        setting = setting_type()
        setting.asset_automation_type = getattr(
            client.enums.AssetAutomationTypeEnum, s_type
        )
        setting.asset_automation_status = getattr(
            client.enums.AssetAutomationStatusEnum, s_status
        )
        campaign.asset_automation_settings.append(setting)
    # Champ repete : la liste envoyee remplace integralement l'ancienne.
    op.update_mask.paths.append("asset_automation_settings")
    return op


def count_ops(ops: list) -> dict[str, int]:
    """Compte les MutateOperation par type ('campaign', 'ad_group', ...)."""
    counts: dict[str, int] = {}
    for op in ops:
        kind = (op._pb.WhichOneof("operation") or "unknown").removesuffix("_operation")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def build_mutate_request(client, cid: str, ops: list, dry_run: bool):
    """MutateGoogleAdsRequest avec toutes les operations (validate_only si dry_run)."""
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    for op in ops:
        request.mutate_operations.append(op)
    request.validate_only = bool(dry_run)
    return request


def mutate_atomic(client, cid: str, ops: list, dry_run: bool):
    """Envoie toutes les operations en UNE requete GoogleAdsService.mutate.

    Sans partial_failure : tout est cree, ou rien ne l'est. Avec dry_run, la
    requete est seulement validee (validate_only) et ne renvoie aucun resultat.
    """
    svc = client.get_service("GoogleAdsService")
    return svc.mutate(request=build_mutate_request(client, cid, ops, dry_run))


def created_resource_names(response) -> dict[str, list[str]]:
    """Regroupe les resource names crees par type ('campaign', 'ad_group', ...)."""
    out: dict[str, list[str]] = {}
    for result in response.mutate_operation_responses:
        kind = result._pb.WhichOneof("response")
        if not kind:
            continue
        out.setdefault(kind.removesuffix("_result"), []).append(
            getattr(result, kind).resource_name
        )
    return out


# ---------------------------------------------------------------------------
# Customer Match : normalisation + hachage des membres (regles Google) et
# operations OfflineUserDataJob (fonctions pures, testables hors ligne)
# ---------------------------------------------------------------------------

# Duree de vie maximale d'un membre d'une liste Customer Match (jours). Google
# n'accepte plus les valeurs superieures (ni "sans expiration") pour ces listes.
CUSTOMER_MATCH_MAX_LIFE_SPAN_DAYS = 540
# Operations par AddOfflineUserDataJobOperationsRequest (lots).
CUSTOMER_MATCH_OPS_PER_REQUEST = 1000
# Membres acceptes par appel d'outil (taille de la charge utile MCP).
CUSTOMER_MATCH_MAX_MEMBERS = 5000
CUSTOMER_MATCH_IDENTIFIER_TYPES = ("hashed_email", "hashed_phone_number", "address_info")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Cles acceptees pour un membre (dont les en-tetes du gabarit Google :
# Email, Phone, First Name, Last Name, Country, Zip) -> cle canonique.
MEMBER_KEY_ALIASES = {
    "email": "email", "e_mail": "email", "email_address": "email", "courriel": "email",
    "phone": "phone", "phone_number": "phone", "mobile": "phone", "telephone": "phone",
    "téléphone": "phone", "tel": "phone",
    "first_name": "first_name", "firstname": "first_name", "given_name": "first_name",
    "prenom": "first_name", "prénom": "first_name",
    "last_name": "last_name", "lastname": "last_name", "family_name": "last_name",
    "surname": "last_name", "nom": "last_name",
    "country": "country_code", "country_code": "country_code", "pays": "country_code",
    "zip": "postal_code", "zip_code": "postal_code", "zipcode": "postal_code",
    "postal_code": "postal_code", "postcode": "postal_code", "code_postal": "postal_code",
}

# Quelques noms de pays frequents -> code ISO 3166-1 alpha-2.
COUNTRY_ALIASES = {
    "CANADA": "CA", "CAN": "CA", "USA": "US", "UNITED STATES": "US",
    "ETATS-UNIS": "US", "ÉTATS-UNIS": "US", "FRANCE": "FR", "FRA": "FR",
    "BELGIQUE": "BE", "BELGIUM": "BE", "SUISSE": "CH", "SWITZERLAND": "CH",
    "UNITED KINGDOM": "GB", "UK": "GB", "ROYAUME-UNI": "GB", "MEXIQUE": "MX",
    "MEXICO": "MX",
}


def sha256_hex(text: str) -> str:
    """SHA-256 hexadecimal (minuscules) d'une chaine deja normalisee (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_email(value: Any) -> Optional[str]:
    """Regle Google : espaces de bord retires, minuscules ; pour gmail.com et
    googlemail.com, points de la partie locale supprimes. None si inutilisable."""
    email = str(value or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return None
    local, domain = email.rsplit("@", 1)
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


def _is_nanp(digits: str) -> bool:
    """10 chiffres au format nord-americain NXX NXX XXXX (N = 2..9)."""
    return len(digits) == 10 and digits[0] in "23456789" and digits[3] in "23456789"


def normalize_phone(value: Any) -> Optional[str]:
    """Format E.164 : '+' suivi de 7 a 15 chiffres, rien d'autre.

    Sans '+' : prefixe international '00' (ou '011' nord-americain) -> '+' ;
    10 chiffres nord-americains -> '+1' + numero ; 11 chiffres commencant par
    1 -> '+' + numero. Tout autre numero sans indicatif pays est inutilisable
    (None). Une extension finale ("ext 12", "x12", "poste 12", "#12") est
    retiree.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = re.sub(r"(?i)\s*(?:ext\.?|extension|poste|x|#)\s*\d{1,6}\s*$", "", raw)
    digits = re.sub(r"[^0-9]", "", raw)
    if raw.startswith("+"):
        number = digits
    elif digits.startswith("011") and len(digits) >= 10:
        number = digits[3:]
    elif digits.startswith("00") and len(digits) >= 9:
        number = digits[2:]
    elif len(digits) == 11 and digits[0] == "1" and _is_nanp(digits[1:]):
        number = digits
    elif _is_nanp(digits):
        number = "1" + digits
    else:
        return None
    if not 7 <= len(number) <= 15 or number[0] == "0":
        return None
    return "+" + number


def normalize_name(value: Any) -> str:
    """Regle Google pour prenom / nom : espaces de bord retires, minuscules ;
    les accents sont conserves."""
    return str(value or "").strip().lower()


def normalize_country_code(value: Any) -> Optional[str]:
    """Code pays ISO 3166-1 alpha-2 en majuscules, ou None."""
    code = str(value or "").strip().upper()
    code = COUNTRY_ALIASES.get(code, code)
    return code if re.fullmatch(r"[A-Z]{2}", code) else None


def normalize_postal_code(value: Any) -> str:
    """Code postal : espaces de bord retires, majuscules (sinon tel quel)."""
    return str(value or "").strip().upper()


def _canonical_member(member: Any, label: str) -> dict:
    """Ramene les cles d'un membre aux cles canoniques ; valeurs vides ignorees."""
    if not isinstance(member, dict):
        raise SpecError(
            f"{label} : objet {{'email', 'phone', 'first_name', 'last_name', "
            "'country_code', 'postal_code'}} attendu."
        )
    out: dict[str, Any] = {}
    for key, value in member.items():
        canon = MEMBER_KEY_ALIASES.get(re.sub(r"[\s\-]+", "_", str(key).strip().lower()))
        if canon and value is not None and str(value).strip():
            out[canon] = value
    return out


def normalize_member(member: Any, label: str = "member") -> dict:
    """Normalise et hache un membre selon les regles Customer Match de Google.

    Retourne {"identifiers": {hashed_email, hashed_phone_number, address_info},
    "invalid": [champs inutilisables], "partial_address": bool}. L'identifiant
    address_info n'est construit que si prenom, nom, pays ET code postal sont
    tous presents (et valides).
    """
    data = _canonical_member(member, label)
    identifiers: dict[str, Any] = {}
    invalid: list[str] = []
    if "email" in data:
        email = normalize_email(data["email"])
        if email:
            identifiers["hashed_email"] = sha256_hex(email)
        else:
            invalid.append("email")
    if "phone" in data:
        phone = normalize_phone(data["phone"])
        if phone:
            identifiers["hashed_phone_number"] = sha256_hex(phone)
        else:
            invalid.append("phone")
    partial_address = False
    if any(k in data for k in ("first_name", "last_name", "country_code", "postal_code")):
        first = normalize_name(data.get("first_name"))
        last = normalize_name(data.get("last_name"))
        country = normalize_country_code(data.get("country_code"))
        postal = normalize_postal_code(data.get("postal_code"))
        if "country_code" in data and country is None:
            invalid.append("country_code")
        if first and last and country and postal:
            identifiers["address_info"] = {
                "hashed_first_name": sha256_hex(first),
                "hashed_last_name": sha256_hex(last),
                "country_code": country,
                "postal_code": postal,
            }
        else:
            partial_address = True
    return {"identifiers": identifiers, "invalid": invalid, "partial_address": partial_address}


def prepare_customer_match_members(members: Any) -> dict:
    """Normalise/hache toute une liste de membres, sans appel a l'API.

    Retourne {"members": [identifiants haches par membre exploitable],
    "counts": {...}, "skipped_positions": [positions (1-based) des membres
    sans identifiant, 20 max]}. Les doublons exacts (memes identifiants) ne
    sont gardes qu'une fois.
    """
    if not isinstance(members, (list, tuple)) or not members:
        raise SpecError("members : liste non vide d'objets attendue.")
    if len(members) > CUSTOMER_MATCH_MAX_MEMBERS:
        raise SpecError(
            f"members : {len(members)} membres, maximum {CUSTOMER_MATCH_MAX_MEMBERS} "
            "par appel (decouper en plusieurs appels)."
        )
    prepared: list[dict] = []
    seen: set[str] = set()
    counts: dict[str, Any] = {
        "members_received": len(members),
        "members_to_upload": 0,
        "members_skipped_no_identifier": 0,
        "members_skipped_duplicate": 0,
        "members_with_incomplete_address": 0,
        "identifiers": {t: 0 for t in CUSTOMER_MATCH_IDENTIFIER_TYPES},
        "invalid_values": {},
        "requests_needed": 0,
    }
    skipped_positions: list[int] = []
    for i, member in enumerate(members, 1):
        info = normalize_member(member, f"members[{i}]")
        for field in info["invalid"]:
            counts["invalid_values"][field] = counts["invalid_values"].get(field, 0) + 1
        if info["partial_address"]:
            counts["members_with_incomplete_address"] += 1
        identifiers = info["identifiers"]
        if not identifiers:
            counts["members_skipped_no_identifier"] += 1
            if len(skipped_positions) < 20:
                skipped_positions.append(i)
            continue
        key = sha256_hex(json.dumps(identifiers, sort_keys=True))
        if key in seen:
            counts["members_skipped_duplicate"] += 1
            continue
        seen.add(key)
        for t in identifiers:
            counts["identifiers"][t] += 1
        prepared.append(identifiers)
    counts["members_to_upload"] = len(prepared)
    counts["requests_needed"] = (
        len(prepared) + CUSTOMER_MATCH_OPS_PER_REQUEST - 1
    ) // CUSTOMER_MATCH_OPS_PER_REQUEST
    return {"members": prepared, "counts": counts, "skipped_positions": skipped_positions}


def _build_user_list_op(client, name: str, description: str, life_span_days: int):
    """UserListOperation (create) d'une liste Customer Match CONTACT_INFO."""
    op = client.get_type("UserListOperation")
    user_list = op.create
    user_list.name = name
    if description:
        user_list.description = description
    user_list.membership_status = client.enums.UserListMembershipStatusEnum.OPEN
    user_list.membership_life_span = int(life_span_days)
    crm = user_list.crm_based_user_list
    crm.upload_key_type = client.enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO
    crm.data_source_type = client.enums.UserListCrmDataSourceTypeEnum.FIRST_PARTY
    return op


def _build_offline_user_data_job(client, user_list_rn: str):
    """OfflineUserDataJob CUSTOMER_MATCH_USER_LIST cible sur `user_list_rn`.

    Consentement (ad_user_data / ad_personalization) declare GRANTED au niveau
    du job : customer_match_user_list_metadata.consent.
    """
    job = client.get_type("OfflineUserDataJob")
    job.type_ = client.enums.OfflineUserDataJobTypeEnum.CUSTOMER_MATCH_USER_LIST
    metadata = job.customer_match_user_list_metadata
    metadata.user_list = user_list_rn
    metadata.consent.ad_user_data = client.enums.ConsentStatusEnum.GRANTED
    metadata.consent.ad_personalization = client.enums.ConsentStatusEnum.GRANTED
    return job


def _add_user_identifiers(client, user_data, identifiers: dict) -> None:
    """Ajoute a `user_data` (UserData) un UserIdentifier par identifiant hache."""
    if "hashed_email" in identifiers:
        ident = client.get_type("UserIdentifier")
        ident.hashed_email = identifiers["hashed_email"]
        user_data.user_identifiers.append(ident)
    if "hashed_phone_number" in identifiers:
        ident = client.get_type("UserIdentifier")
        ident.hashed_phone_number = identifiers["hashed_phone_number"]
        user_data.user_identifiers.append(ident)
    if "address_info" in identifiers:
        address = identifiers["address_info"]
        ident = client.get_type("UserIdentifier")
        ident.address_info.hashed_first_name = address["hashed_first_name"]
        ident.address_info.hashed_last_name = address["hashed_last_name"]
        ident.address_info.country_code = address["country_code"]
        ident.address_info.postal_code = address["postal_code"]
        user_data.user_identifiers.append(ident)


def _build_add_operations_requests(
    client,
    job_rn: str,
    members: list[dict],
    remove: bool = False,
    batch_size: int = CUSTOMER_MATCH_OPS_PER_REQUEST,
):
    """Generateur : une AddOfflineUserDataJobOperationsRequest par lot de
    `batch_size` membres (enable_partial_failure), construite a la demande
    pour garder une empreinte memoire modeste."""
    for start in range(0, len(members), batch_size):
        request = client.get_type("AddOfflineUserDataJobOperationsRequest")
        request.resource_name = job_rn
        request.enable_partial_failure = True
        for identifiers in members[start : start + batch_size]:
            op = client.get_type("OfflineUserDataJobOperation")
            _add_user_identifiers(client, op.remove if remove else op.create, identifiers)
            request.operations.append(op)
        yield request


def summarize_partial_failure(client, status) -> Optional[dict]:
    """Resume le partial_failure_error (google.rpc.Status) d'une reponse
    AddOfflineUserDataJobOperations ; None s'il n'y a aucune erreur."""
    if status is None or not getattr(status, "code", 0):
        return None
    failure_type = type(client.get_type("GoogleAdsFailure"))
    errors: list[dict] = []
    failed_ops: set[int] = set()
    for detail in getattr(status, "details", []):
        try:
            failure = failure_type.deserialize(detail.value)
        except Exception:
            continue
        for err in failure.errors:
            index = None
            for element in err.location.field_path_elements:
                if element.field_name == "operations" and "index" in element:
                    index = element.index
                    break
            if index is not None:
                failed_ops.add(index)
            if len(errors) < 10:
                errors.append(
                    {
                        "message": err.message,
                        "code": str(err.error_code).strip(),
                        "operation_index": index,
                    }
                )
    return {
        "failed_operations": len(failed_ops),
        "errors": errors,
        "message": getattr(status, "message", ""),
    }


def _job_resource_name(cid: str, value: Any) -> str:
    """Resource name d'un OfflineUserDataJob a partir du nom complet ou de l'id."""
    text = str(value or "").strip()
    match = re.fullmatch(r"customers/(\d+)/offlineUserDataJobs/(\d+)", text)
    if match:
        if match.group(1) != cid:
            raise SpecError(
                f"job_resource_name appartient au compte {match.group(1)}, pas a {cid}."
            )
        return text
    if text.isdigit():
        return f"customers/{cid}/offlineUserDataJobs/{text}"
    raise SpecError(
        "job_resource_name : format attendu customers/<id client>/"
        "offlineUserDataJobs/<id job>, ou l'id numerique du job."
    )


def _int_or_none(value: Any) -> Optional[int]:
    """Les int64 GAQL arrivent sous forme de chaines ; None si absent."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def user_list_summary(u: dict) -> dict:
    """Resume lisible d'une ligne GAQL user_list."""
    return {
        "id": str(u["id"]) if u.get("id") is not None else None,
        "name": u.get("name"),
        "type": u.get("type"),
        "membership_status": u.get("membership_status"),
        "size_for_search": _int_or_none(u.get("size_for_search")),
        "size_for_display": _int_or_none(u.get("size_for_display")),
        "eligible_for_search": u.get("eligible_for_search"),
        "eligible_for_display": u.get("eligible_for_display"),
        "match_rate_percentage": _int_or_none(u.get("match_rate_percentage")),
        "upload_key_type": u.get("crm_based_user_list", {}).get("upload_key_type"),
    }


def offline_job_summary(j: dict) -> dict:
    """Resume lisible d'une ligne GAQL offline_user_data_job."""
    return {
        "resource_name": j.get("resource_name"),
        "id": str(j["id"]) if j.get("id") is not None else None,
        "type": j.get("type"),
        "status": j.get("status"),
        "failure_reason": j.get("failure_reason"),
        "user_list": j.get("customer_match_user_list_metadata", {}).get("user_list"),
        "match_rate_range": j.get("operation_metadata", {}).get("match_rate_range"),
    }


USER_LIST_FIELDS = (
    "user_list.resource_name, user_list.id, user_list.name, user_list.description, "
    "user_list.type, user_list.membership_status, user_list.membership_life_span, "
    "user_list.size_for_search, user_list.size_for_display, "
    "user_list.size_range_for_search, user_list.size_range_for_display, "
    "user_list.eligible_for_search, user_list.eligible_for_display, "
    "user_list.match_rate_percentage, user_list.read_only, "
    "user_list.crm_based_user_list.upload_key_type"
)
OFFLINE_JOB_FIELDS = (
    "offline_user_data_job.resource_name, offline_user_data_job.id, "
    "offline_user_data_job.type, offline_user_data_job.status, "
    "offline_user_data_job.failure_reason, "
    "offline_user_data_job.customer_match_user_list_metadata.user_list, "
    "offline_user_data_job.operation_metadata.match_rate_range"
)
CUSTOMER_MATCH_PROCESSING_NOTE = (
    "Google traite le job de facon asynchrone : generalement 6 a 12 h, jusqu'a "
    "48 h. Suivre avec get_customer_match_status (job_resource_name) ; la taille "
    "de la liste et son taux de correspondance (match_rate_percentage) "
    "apparaissent ensuite sur la liste. Une liste doit atteindre le minimum "
    "Google (1 000 membres correspondants pour Search, Shopping et YouTube) "
    "avant de pouvoir diffuser."
)


def _find_user_list_by_name(cid: str, name: str) -> Optional[dict]:
    """Liste d'audience portant ce nom (exact, sinon sans tenir compte de la
    casse), ou None. Parcourt les listes du compte (1000 max)."""
    rows = run_query(
        cid,
        "SELECT user_list.resource_name, user_list.id, user_list.name, "
        "user_list.type, user_list.membership_status, "
        "user_list.crm_based_user_list.upload_key_type FROM user_list",
        limit=1000,
    )
    lists = [r.get("user_list", {}) for r in rows]
    for u in lists:
        if u.get("name") == name:
            return u
    lower = name.lower()
    for u in lists:
        if str(u.get("name") or "").lower() == lower:
            return u
    return None


# ---------------------------------------------------------------------------
# Serveur MCP et outils
# ---------------------------------------------------------------------------

AUTH_SCOPE_KEY = "lbc_mcp_user"  # cle d'acces authentifiee, ajoutee au scope ASGI
READ_TOOL_NAMES: set[str] = set()
WRITE_TOOL_NAMES: set[str] = set()


def log(message: str) -> None:
    print(message, flush=True)


def can_write(user: Optional[dict]) -> bool:
    """Les outils d'ecriture exigent une cle full ET GOOGLE_ADS_ALLOW_WRITES=true."""
    return bool(ALLOW_WRITES and user and user.get("role") == "full")


class RoleAwareFastMCP(FastMCP):
    """FastMCP dont la liste d'outils et les appels dependent du role de la cle
    d'acces de la requete (full | read), comme sur le serveur Meta Ads."""

    def current_user(self) -> Optional[dict]:
        """Cle d'acces de la requete en cours (posee dans le scope par `app`)."""
        try:
            request = self._mcp_server.request_context.request
        except LookupError:
            return None  # hors requete HTTP : aucun droit d'ecriture
        return (getattr(request, "scope", None) or {}).get(AUTH_SCOPE_KEY)

    async def list_all_tools(self) -> list:
        """Tous les outils enregistres, sans filtre de role (tests, diagnostic)."""
        return await super().list_tools()

    async def list_tools(self) -> list:
        tools = await super().list_tools()
        if can_write(self.current_user()):
            return tools
        return [t for t in tools if t.name not in WRITE_TOOL_NAMES]

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        user = self.current_user()
        if name in WRITE_TOOL_NAMES and not can_write(user):
            raise ToolError(
                f"Outil {name} non autorise : "
                + (
                    "cette cle d'acces est en lecture seule (role read)."
                    if ALLOW_WRITES
                    else "les modifications sont desactivees sur ce serveur "
                    "(GOOGLE_ADS_ALLOW_WRITES)."
                )
            )
        dry = " (dry run)" if (arguments or {}).get("dry_run") else ""
        log(f"[{name}]{dry} par {(user or {}).get('name', '?')}")
        return await super().call_tool(name, arguments)


def _instructions() -> str:
    parts = []
    if BRAND:
        parts.append(f"MCP server for the Google Ads account of {BRAND}.")
    else:
        parts.append("MCP server for the user's Google Ads account.")
    if SERVED_CUSTOMER_IDS:
        parts.append(
            f"Default account: customer_id {SERVED_CUSTOMER_IDS[0]}; customer_id "
            "can be omitted in every tool."
        )
        if len(SERVED_CUSTOMER_IDS) > 1:
            parts.append(
                "Other accounts served: " + ", ".join(SERVED_CUSTOMER_IDS[1:]) + "."
            )
    else:
        parts.append(
            "Start with list_accounts to discover accessible accounts and pass "
            "customer_id to the other tools."
        )
    return " ".join(parts) + " "


mcp = RoleAwareFastMCP(
    SERVICE_NAME,
    instructions=(
        _instructions() +
        "All monetary *_micros fields are in millionths of the account currency "
        "(divide by 1,000,000). Customer IDs are 10 digits (dashes optional). "
        "For anything not covered by a dedicated tool, use run_gaql with a "
        "Google Ads Query Language (GAQL) query. Performance Max campaigns: "
        "list_asset_groups (asset groups, search themes, audience signals). "
        "Conversion goals: get_campaign_conversion_goals. Audiences / Customer "
        "Match: list_user_lists, get_customer_match_status (read). Write tools "
        "(set_campaign_status, set_campaign_budget, add_negative_keywords, "
        "set_campaign_target_roas, set_campaign_conversion_goals, "
        "add_search_themes, set_campaign_url_expansion, create_search_campaign, "
        "add_search_ad_group, add_campaign_assets, create_customer_match_list, "
        "upload_customer_match_members) modify a live advertising account that "
        "spends real money: always confirm with the user before calling them, "
        "and prefer dry_run=true first when unsure. create_search_campaign "
        "builds a whole Search campaign (budget, targeting, ad groups, "
        "responsive search ads, keywords, sitelinks, callouts) in one atomic "
        "request, PAUSED by default. upload_customer_match_members takes raw "
        "contact data (email, phone, first_name, last_name, country_code, "
        "postal_code), normalises and SHA-256 hashes it on the server per "
        "Google's rules, and uploads it through an OfflineUserDataJob; its "
        "dry_run=true does all the normalisation and counting without calling "
        "Google at all. Uploads are processed asynchronously (6-48 h). Write "
        "tools are only listed for full-role access keys, when writes are "
        "enabled on the server."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TRANSPORT_SECURITY,
)
mcp._mcp_server.version = VERSION  # serverInfo.version (initialize)


def read_tool(fn):
    """Enregistre un outil de lecture (visible par toutes les cles)."""
    READ_TOOL_NAMES.add(fn.__name__)
    return mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))(fn)


def write_tool(fn):
    """Enregistre un outil d'ecriture (cle full + GOOGLE_ADS_ALLOW_WRITES=true)."""
    WRITE_TOOL_NAMES.add(fn.__name__)
    return mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))(fn)


@read_tool
def list_accounts() -> str:
    """List the Google Ads accounts this server can access.

    Returns the accounts directly accessible by the authenticated user and,
    if a manager (MCC) account is configured via GOOGLE_ADS_LOGIN_CUSTOMER_ID,
    the client accounts under that manager. When the server is limited to its
    brand's account(s) (GOOGLE_ADS_CUSTOMER_ID), only those (and the manager)
    are listed, and default_customer_id is the account the other tools use
    when customer_id is omitted.
    """
    try:
        client = get_client()
        result: dict[str, Any] = {"directly_accessible": [], "under_manager": []}
        login_cid = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
        if SERVED_CUSTOMER_IDS:
            result["default_customer_id"] = SERVED_CUSTOMER_IDS[0]
            result["served_customer_ids"] = list(SERVED_CUSTOMER_IDS)

        def shown(cid: str) -> bool:
            return not SERVED_CUSTOMER_IDS or cid in SERVED_CUSTOMER_IDS or cid == login_cid

        hidden: set[str] = set()
        customer_service = client.get_service("CustomerService")
        accessible = customer_service.list_accessible_customers()
        for resource_name in accessible.resource_names:
            cid = resource_name.split("/")[-1]
            if not shown(cid):
                hidden.add(cid)
                continue
            entry: dict[str, Any] = {"customer_id": cid}
            try:
                rows = run_query(
                    cid,
                    "SELECT customer.descriptive_name, customer.currency_code, "
                    "customer.time_zone, customer.manager, customer.test_account "
                    "FROM customer",
                    limit=1,
                )
                if rows:
                    c = rows[0].get("customer", {})
                    entry.update(
                        name=c.get("descriptive_name"),
                        currency=c.get("currency_code"),
                        time_zone=c.get("time_zone"),
                        is_manager=c.get("manager", False),
                        is_test_account=c.get("test_account", False),
                    )
            except Exception as ex:  # compte non actif, droits partiels, etc.
                entry["note"] = f"details indisponibles : {type(ex).__name__}"
            result["directly_accessible"].append(entry)

        if login_cid:
            try:
                rows = run_query(
                    login_cid,
                    "SELECT customer_client.id, customer_client.descriptive_name, "
                    "customer_client.level, customer_client.manager, "
                    "customer_client.status, customer_client.currency_code "
                    "FROM customer_client WHERE customer_client.level <= 1",
                    limit=200,
                )
                for r in rows:
                    cc = r.get("customer_client", {})
                    if not shown(str(cc.get("id"))):
                        hidden.add(str(cc.get("id")))
                        continue
                    result["under_manager"].append(
                        {
                            "customer_id": str(cc.get("id")),
                            "name": cc.get("descriptive_name"),
                            "level": cc.get("level", 0),
                            "is_manager": cc.get("manager", False),
                            "status": cc.get("status"),
                            "currency": cc.get("currency_code"),
                        }
                    )
            except Exception as ex:
                result["under_manager_error"] = str(ex)

        if hidden:
            result["other_accounts_hidden"] = len(hidden)
            result["note"] = (
                "Comptes accessibles mais non servis par ce serveur "
                "(GOOGLE_ADS_CUSTOMER_ID) : masques."
            )
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def run_gaql(query: str, limit: int = 200, customer_id: Optional[str] = None) -> str:
    """Run any GAQL (Google Ads Query Language) query against an account.

    The universal read tool: campaigns, ad groups, ads, keywords, search
    terms, audiences, budgets, change history, recommendations, etc.

    Args:
        query: GAQL query. Examples:
          - SELECT campaign.id, campaign.name, campaign.status,
            metrics.impressions, metrics.clicks, metrics.cost_micros
            FROM campaign WHERE segments.date DURING LAST_30_DAYS
            ORDER BY metrics.cost_micros DESC
          - SELECT search_term_view.search_term, metrics.clicks,
            metrics.conversions, metrics.cost_micros FROM search_term_view
            WHERE segments.date DURING LAST_7_DAYS
          - SELECT change_event.change_date_time, change_event.change_resource_type,
            change_event.user_email FROM change_event
            WHERE change_event.change_date_time DURING LAST_14_DAYS LIMIT 50
        limit: max rows returned (default 200, max 1000).
        customer_id: optional 10-digit account ID; defaults to the server's account.

    Monetary *_micros fields are millionths of the account currency.
    """
    try:
        limit = max(1, min(int(limit), 1000))
        rows = run_query(resolve_cid(customer_id), query, limit=limit)
        return ok({"row_count": len(rows), "truncated_at": limit, "rows": rows})
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def get_campaigns(customer_id: Optional[str] = None) -> str:
    """List all campaigns of an account with status, type, budget and dates.

    Includes the campaign budget (daily amount in account currency and in
    micros, and whether it is shared between campaigns).

    Args:
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        rows = run_query(
            resolve_cid(customer_id),
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
            "campaign.start_date_time, campaign.end_date_time, campaign.serving_status, "
            "campaign.campaign_budget, campaign_budget.id, "
            "campaign_budget.amount_micros, campaign_budget.explicitly_shared "
            "FROM campaign ORDER BY campaign.status ASC, campaign.name ASC",
            limit=500,
        )
        campaigns = []
        for r in rows:
            c = r.get("campaign", {})
            b = r.get("campaign_budget", {})
            campaigns.append(
                {
                    "id": str(c.get("id")),
                    "name": c.get("name"),
                    "status": c.get("status"),
                    "serving_status": c.get("serving_status"),
                    "channel_type": c.get("advertising_channel_type"),
                    "bidding_strategy": c.get("bidding_strategy_type"),
                    "start_date": (c.get("start_date_time") or "")[:10] or None,
                    "end_date": (c.get("end_date_time") or "")[:10] or None,
                    "budget_id": str(b.get("id")) if b.get("id") else None,
                    "daily_budget": micros_to_unit(b.get("amount_micros")),
                    "daily_budget_micros": b.get("amount_micros"),
                    "budget_is_shared": b.get("explicitly_shared", False),
                }
            )
        return ok({"campaign_count": len(campaigns), "campaigns": campaigns})
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def get_campaign_performance(
    last_n_days: int = 30, customer_id: Optional[str] = None
) -> str:
    """Per-campaign performance metrics over the last N days.

    Returns impressions, clicks, CTR, average CPC, cost, conversions,
    conversion value and cost per conversion for each campaign that had
    traffic, ordered by cost (highest first).

    Args:
        last_n_days: period length in days, ending today (default 30).
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        rows = run_query(
            resolve_cid(customer_id),
            "SELECT campaign.id, campaign.name, campaign.status, "
            "metrics.impressions, metrics.clicks, metrics.ctr, "
            "metrics.average_cpc, metrics.cost_micros, metrics.conversions, "
            "metrics.conversions_value, metrics.cost_per_conversion "
            f"FROM campaign WHERE {date_clause(last_n_days)} "
            "AND metrics.impressions > 0 ORDER BY metrics.cost_micros DESC",
            limit=500,
        )
        out = []
        for r in rows:
            c, m = r.get("campaign", {}), r.get("metrics", {})
            out.append(
                {
                    "id": str(c.get("id")),
                    "name": c.get("name"),
                    "status": c.get("status"),
                    "impressions": int(m.get("impressions", 0)),
                    "clicks": int(m.get("clicks", 0)),
                    "ctr_pct": round(float(m.get("ctr", 0)) * 100, 2),
                    "avg_cpc": micros_to_unit(m.get("average_cpc")),
                    "cost": micros_to_unit(m.get("cost_micros")),
                    "conversions": round(float(m.get("conversions", 0)), 2),
                    "conversions_value": round(
                        float(m.get("conversions_value", 0)), 2
                    ),
                    "cost_per_conversion": micros_to_unit(
                        m.get("cost_per_conversion")
                    ),
                }
            )
        return ok(
            {
                "period_days": last_n_days,
                "campaign_count": len(out),
                "campaigns": out,
                "note": "Montants dans la devise du compte.",
            }
        )
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def get_keyword_performance(
    last_n_days: int = 30, limit: int = 100, customer_id: Optional[str] = None
) -> str:
    """Top keywords by cost over the last N days, with quality score.

    Returns keyword text, match type, ad group, campaign, metrics and
    quality score info, ordered by cost (highest first).

    Args:
        last_n_days: period length in days, ending today (default 30).
        limit: max keywords returned (default 100, max 500).
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        rows = run_query(
            resolve_cid(customer_id),
            "SELECT campaign.name, ad_group.name, "
            "ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
            "ad_group_criterion.quality_info.quality_score, "
            "metrics.impressions, metrics.clicks, metrics.cost_micros, "
            "metrics.conversions, metrics.average_cpc "
            f"FROM keyword_view WHERE {date_clause(last_n_days)} "
            "AND metrics.impressions > 0 ORDER BY metrics.cost_micros DESC",
            limit=max(1, min(int(limit), 500)),
        )
        out = []
        for r in rows:
            crit = r.get("ad_group_criterion", {})
            kw = crit.get("keyword", {})
            m = r.get("metrics", {})
            out.append(
                {
                    "keyword": kw.get("text"),
                    "match_type": kw.get("match_type"),
                    "status": crit.get("status"),
                    "quality_score": crit.get("quality_info", {}).get(
                        "quality_score"
                    ),
                    "campaign": r.get("campaign", {}).get("name"),
                    "ad_group": r.get("ad_group", {}).get("name"),
                    "impressions": int(m.get("impressions", 0)),
                    "clicks": int(m.get("clicks", 0)),
                    "cost": micros_to_unit(m.get("cost_micros")),
                    "avg_cpc": micros_to_unit(m.get("average_cpc")),
                    "conversions": round(float(m.get("conversions", 0)), 2),
                }
            )
        return ok({"period_days": last_n_days, "keyword_count": len(out), "keywords": out})
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def get_search_terms(
    last_n_days: int = 30, limit: int = 100, customer_id: Optional[str] = None
) -> str:
    """Actual search terms that triggered ads over the last N days.

    Useful to find wasted spend and negative keyword candidates. Ordered by
    cost (highest first).

    Args:
        last_n_days: period length in days, ending today (default 30).
        limit: max search terms returned (default 100, max 500).
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        rows = run_query(
            resolve_cid(customer_id),
            "SELECT search_term_view.search_term, search_term_view.status, "
            "campaign.name, ad_group.name, metrics.impressions, metrics.clicks, "
            "metrics.cost_micros, metrics.conversions "
            f"FROM search_term_view WHERE {date_clause(last_n_days)} "
            "ORDER BY metrics.cost_micros DESC",
            limit=max(1, min(int(limit), 500)),
        )
        out = []
        for r in rows:
            st = r.get("search_term_view", {})
            m = r.get("metrics", {})
            out.append(
                {
                    "search_term": st.get("search_term"),
                    "status": st.get("status"),
                    "campaign": r.get("campaign", {}).get("name"),
                    "ad_group": r.get("ad_group", {}).get("name"),
                    "impressions": int(m.get("impressions", 0)),
                    "clicks": int(m.get("clicks", 0)),
                    "cost": micros_to_unit(m.get("cost_micros")),
                    "conversions": round(float(m.get("conversions", 0)), 2),
                }
            )
        return ok({"period_days": last_n_days, "term_count": len(out), "search_terms": out})
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def list_asset_groups(campaign_id: str, customer_id: Optional[str] = None) -> str:
    """List the asset groups of a Performance Max campaign with their signals.

    For each asset group: id, name, status, final URLs, display paths (path1,
    path2), ad strength, plus its search themes and audience signals
    (asset_group_signal). Asset groups only exist in Performance Max
    campaigns; for other campaign types the list is empty.

    Args:
        campaign_id: numeric campaign ID (from get_campaigns).
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        cid = resolve_cid(customer_id)
        camp_id = int(campaign_id)
        rows = run_query(
            cid,
            "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
            "asset_group.id, asset_group.name, asset_group.status, "
            "asset_group.final_urls, asset_group.path1, asset_group.path2, "
            "asset_group.ad_strength FROM asset_group "
            f"WHERE campaign.id = {camp_id} ORDER BY asset_group.name",
            limit=500,
        )
        if rows:
            c = rows[0].get("campaign", {})
        else:
            row = campaign_row(
                cid,
                campaign_id,
                "campaign.id, campaign.name, campaign.advertising_channel_type",
            )
            if row is None:
                return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")
            c = row.get("campaign", {})
        campaign_info = {
            "id": str(c.get("id")),
            "name": c.get("name"),
            "channel_type": c.get("advertising_channel_type"),
        }

        groups: dict[str, dict] = {}
        for r in rows:
            g = r.get("asset_group", {})
            gid = str(g.get("id"))
            groups[gid] = {
                "id": gid,
                "name": g.get("name"),
                "status": g.get("status"),
                "final_urls": g.get("final_urls", []),
                "path1": g.get("path1"),
                "path2": g.get("path2"),
                "ad_strength": g.get("ad_strength"),
                "search_themes": [],
                "audience_signals": [],
            }

        if groups:
            signal_rows = run_query(
                cid,
                "SELECT asset_group.id, asset_group_signal.search_theme.text, "
                "asset_group_signal.audience.audience, "
                "asset_group_signal.approval_status FROM asset_group_signal "
                f"WHERE campaign.id = {camp_id}",
                limit=1000,
            )
            audience_names: dict[str, str] = {}
            if any("audience" in r.get("asset_group_signal", {}) for r in signal_rows):
                try:
                    for r in run_query(
                        cid,
                        "SELECT audience.resource_name, audience.name FROM audience",
                        limit=500,
                    ):
                        a = r.get("audience", {})
                        audience_names[a.get("resource_name", "")] = a.get("name")
                except Exception:
                    pass  # les noms d'audience sont un confort, pas une necessite
            for r in signal_rows:
                gid = str(r.get("asset_group", {}).get("id"))
                signal = r.get("asset_group_signal", {})
                group = groups.get(gid)
                if group is None:
                    continue
                if "search_theme" in signal:
                    group["search_themes"].append(
                        {
                            "text": signal["search_theme"].get("text"),
                            "approval_status": signal.get("approval_status"),
                        }
                    )
                elif "audience" in signal:
                    rn = signal["audience"].get("audience")
                    group["audience_signals"].append(
                        {"resource_name": rn, "name": audience_names.get(rn)}
                    )

        return ok(
            {
                "campaign": campaign_info,
                "asset_group_count": len(groups),
                "asset_groups": list(groups.values()),
                "note": (
                    "Maximum 25 themes de recherche par groupe d'assets. "
                    "Ajouter des themes avec add_search_themes."
                ),
            }
        )
    except Exception as ex:
        return format_google_ads_error(ex)


def _conversion_goal_state(
    cid: str, campaign_id: str
) -> tuple[Optional[dict], dict, list[dict]]:
    """Retourne (campagne, config d'objectifs, objectifs de conversion).

    `campagne` vaut None si la campagne n'existe pas ; `config` est le dict
    conversion_goal_campaign_config (vide si la campagne n'en a pas encore).
    """
    camp_id = int(campaign_id)
    config_rows = run_query(
        cid,
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
        "campaign.bidding_strategy_type, "
        "conversion_goal_campaign_config.goal_config_level, "
        "conversion_goal_campaign_config.custom_conversion_goal "
        f"FROM conversion_goal_campaign_config WHERE campaign.id = {camp_id}",
        limit=1,
    )
    if config_rows:
        campaign = config_rows[0].get("campaign", {})
        config = config_rows[0].get("conversion_goal_campaign_config", {})
    else:
        row = campaign_row(
            cid,
            campaign_id,
            "campaign.id, campaign.name, campaign.advertising_channel_type, "
            "campaign.bidding_strategy_type",
        )
        if row is None:
            return None, {}, []
        campaign, config = row.get("campaign", {}), {}
    goal_rows = run_query(
        cid,
        "SELECT campaign_conversion_goal.category, campaign_conversion_goal.origin, "
        "campaign_conversion_goal.biddable FROM campaign_conversion_goal "
        f"WHERE campaign.id = {camp_id}",
        limit=500,
    )
    goals = [
        {
            "category": g.get("category"),
            "origin": g.get("origin"),
            "biddable": bool(g.get("biddable", False)),
        }
        for g in (r.get("campaign_conversion_goal", {}) for r in goal_rows)
    ]
    goals.sort(key=lambda g: (not g["biddable"], str(g["category"]), str(g["origin"])))
    return campaign, config, goals


@read_tool
def get_campaign_conversion_goals(
    campaign_id: str, customer_id: Optional[str] = None
) -> str:
    """Conversion goals used for bidding by a campaign, vs the account defaults.

    Returns the campaign's goal_config_level (CUSTOMER = the campaign follows
    the account-level goals, CAMPAIGN = campaign-specific goals), the
    campaign_conversion_goal rows (category, origin, biddable) and the
    account-level customer_conversion_goal rows for comparison. "Biddable"
    goals are the ones Smart Bidding optimizes for.

    Args:
        campaign_id: numeric campaign ID (from get_campaigns).
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        cid = resolve_cid(customer_id)
        c, cfg, campaign_goals = _conversion_goal_state(cid, campaign_id)
        if c is None:
            return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")

        account_rows = run_query(
            cid,
            "SELECT customer_conversion_goal.category, "
            "customer_conversion_goal.origin, customer_conversion_goal.biddable "
            "FROM customer_conversion_goal",
            limit=500,
        )
        account_goals = [
            {
                "category": g.get("category"),
                "origin": g.get("origin"),
                "biddable": bool(g.get("biddable", False)),
            }
            for g in (r.get("customer_conversion_goal", {}) for r in account_rows)
        ]
        account_goals.sort(
            key=lambda g: (not g["biddable"], str(g["category"]), str(g["origin"]))
        )
        level = cfg.get("goal_config_level")
        return ok(
            {
                "campaign": {
                    "id": str(c.get("id")),
                    "name": c.get("name"),
                    "channel_type": c.get("advertising_channel_type"),
                    "bidding_strategy": c.get("bidding_strategy_type"),
                },
                "goal_config_level": level,
                "custom_conversion_goal": cfg.get("custom_conversion_goal"),
                "campaign_goals": campaign_goals,
                "campaign_biddable_categories": sorted(
                    {g["category"] for g in campaign_goals if g["biddable"]}
                ),
                "account_goals": account_goals,
                "account_biddable_categories": sorted(
                    {g["category"] for g in account_goals if g["biddable"]}
                ),
                "note": (
                    "Avec goal_config_level = CUSTOMER, ce sont les objectifs du "
                    "compte (account_goals) qui s'appliquent ; avec CAMPAIGN, ceux "
                    "de la campagne (campaign_goals). Modifier avec "
                    "set_campaign_conversion_goals."
                ),
            }
        )
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def list_user_lists(customer_id: Optional[str] = None) -> str:
    """List the audience user lists of an account (Customer Match, remarketing...).

    For each list: id, name, type (CRM_BASED = Customer Match, REMARKETING,
    RULE_BASED, LOGICAL, SIMILAR, LOOKALIKE), membership status (OPEN /
    CLOSED), estimated sizes for Search and Display, eligibility for Search /
    Display, match rate percentage (Customer Match lists only, once computed)
    and upload key type (CONTACT_INFO, CRM_ID, MOBILE_ADVERTISING_ID). Use the
    id with upload_customer_match_members / get_customer_match_status.

    Args:
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        rows = run_query(
            resolve_cid(customer_id),
            f"SELECT {USER_LIST_FIELDS} FROM user_list",
            limit=1000,
        )
        lists = []
        for r in rows:
            u = r.get("user_list", {})
            summary = user_list_summary(u)
            summary["read_only"] = u.get("read_only", False)
            lists.append(summary)
        lists.sort(key=lambda entry: str(entry["name"] or "").lower())
        return ok(
            {
                "user_list_count": len(lists),
                "user_lists": lists,
                "note": (
                    "size_for_search / size_for_display sont des estimations, "
                    "absentes tant que Google n'a pas traite la liste."
                ),
            }
        )
    except Exception as ex:
        return format_google_ads_error(ex)


@read_tool
def get_customer_match_status(
    user_list_id: Optional[str] = None,
    job_resource_name: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> str:
    """Status of a Customer Match list and/or of a member upload job.

    Give user_list_id, job_resource_name, or both (both sections are then
    returned):
      - user_list_id: the list (name, membership status, membership life span,
        estimated sizes and size ranges for Search / Display, eligibility,
        match_rate_percentage, upload key type) plus its most recent upload
        jobs.
      - job_resource_name: the OfflineUserDataJob returned by
        upload_customer_match_members (status PENDING / RUNNING / SUCCESS /
        FAILED, failure reason, type, target list, match rate range once
        computed). Accepts the full resource name
        customers/<cid>/offlineUserDataJobs/<id> or just the numeric job id.

    Processing takes up to 48 hours (usually 6-12 h): before that the job is
    PENDING / RUNNING and the list sizes and match rate are empty.

    Args:
        user_list_id: numeric user list ID (from list_user_lists).
        job_resource_name: offline user data job resource name or numeric id.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        cid = resolve_cid(customer_id)
        if not user_list_id and not job_resource_name:
            return fail("Fournir user_list_id et/ou job_resource_name.")
        result: dict[str, Any] = {}

        if job_resource_name:
            try:
                job_rn = _job_resource_name(cid, job_resource_name)
            except SpecError as ex:
                return fail(str(ex))
            rows = run_query(
                cid,
                f"SELECT {OFFLINE_JOB_FIELDS} FROM offline_user_data_job "
                f"WHERE offline_user_data_job.resource_name = '{job_rn}'",
                limit=1,
            )
            if not rows:
                return fail(f"Job {job_rn} introuvable dans le compte {cid}.")
            result["job"] = offline_job_summary(rows[0].get("offline_user_data_job", {}))

        if user_list_id:
            ul_id = int(user_list_id)
            rows = run_query(
                cid,
                f"SELECT {USER_LIST_FIELDS} FROM user_list WHERE user_list.id = {ul_id}",
                limit=1,
            )
            if not rows:
                return fail(f"Liste d'audience {user_list_id} introuvable dans le compte {cid}.")
            u = rows[0].get("user_list", {})
            summary = user_list_summary(u)
            summary.update(
                resource_name=u.get("resource_name"),
                description=u.get("description"),
                membership_life_span_days=_int_or_none(u.get("membership_life_span")),
                size_range_for_search=u.get("size_range_for_search"),
                size_range_for_display=u.get("size_range_for_display"),
                read_only=u.get("read_only", False),
            )
            result["user_list"] = summary
            user_list_rn = u.get("resource_name") or f"customers/{cid}/userLists/{ul_id}"
            try:
                job_rows = run_query(
                    cid,
                    f"SELECT {OFFLINE_JOB_FIELDS} FROM offline_user_data_job "
                    "WHERE offline_user_data_job.customer_match_user_list_metadata"
                    f".user_list = '{user_list_rn}' "
                    "ORDER BY offline_user_data_job.id DESC LIMIT 10",
                    limit=10,
                )
                result["recent_jobs"] = [
                    offline_job_summary(r.get("offline_user_data_job", {}))
                    for r in job_rows
                ]
            except Exception as ex:  # confort : la liste reste utile sans les jobs
                result["recent_jobs_error"] = f"{type(ex).__name__}: {str(ex)[:300]}"

        result["note"] = CUSTOMER_MATCH_PROCESSING_NOTE
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


# ------------------------------ Outils d'ecriture ---------------------------


@write_tool
def set_campaign_status(
    campaign_id: str,
    status: str,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Pause or enable a campaign. WRITE TOOL - confirm with the user first.

    Args:
        campaign_id: numeric campaign ID (from get_campaigns).
        status: "PAUSED" or "ENABLED".
        dry_run: if true, validates the change without applying it.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        status = status.strip().upper()
        if status not in ("PAUSED", "ENABLED"):
            return fail("status doit etre PAUSED ou ENABLED.")
        client = get_client()
        cid = resolve_cid(customer_id)

        from google.api_core import protobuf_helpers

        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = svc.campaign_path(cid, str(int(campaign_id)))
        campaign.status = getattr(client.enums.CampaignStatusEnum, status)
        client.copy_from(
            op.update_mask, protobuf_helpers.field_mask(None, campaign._pb)
        )

        request = client.get_type("MutateCampaignsRequest")
        request.customer_id = cid
        request.operations.append(op)
        request.validate_only = bool(dry_run)

        response = svc.mutate_campaigns(request=request)
        if dry_run:
            return ok({"dry_run": True, "valid": True, "would_set_status": status})
        return ok(
            {
                "updated": response.results[0].resource_name,
                "new_status": status,
            }
        )
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def set_campaign_budget(
    campaign_id: str,
    daily_budget: float,
    allow_shared_budget: bool = False,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Change a campaign's daily budget. WRITE TOOL - confirm with the user first.

    Args:
        campaign_id: numeric campaign ID.
        daily_budget: new daily amount in the ACCOUNT CURRENCY (e.g. 25.50).
        allow_shared_budget: a shared budget affects several campaigns; the
            tool refuses to touch one unless this is explicitly true.
        dry_run: if true, validates the change without applying it.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        amount = float(daily_budget)
        if amount <= 0:
            return fail("daily_budget doit etre un montant positif.")
        client = get_client()
        cid = resolve_cid(customer_id)

        rows = run_query(
            cid,
            "SELECT campaign.id, campaign.name, campaign.campaign_budget, "
            "campaign_budget.id, campaign_budget.amount_micros, "
            "campaign_budget.explicitly_shared FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)}",
            limit=1,
        )
        if not rows:
            return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")
        b = rows[0].get("campaign_budget", {})
        c = rows[0].get("campaign", {})
        if b.get("explicitly_shared") and not allow_shared_budget:
            return fail(
                {
                    "message": (
                        "Ce budget est PARTAGE entre plusieurs campagnes. "
                        "Modification refusee par precaution. Relancer avec "
                        "allow_shared_budget=true pour forcer."
                    ),
                    "campaign": c.get("name"),
                    "current_daily_budget": micros_to_unit(b.get("amount_micros")),
                }
            )

        from google.api_core import protobuf_helpers

        svc = client.get_service("CampaignBudgetService")
        op = client.get_type("CampaignBudgetOperation")
        budget = op.update
        budget.resource_name = svc.campaign_budget_path(cid, str(b.get("id")))
        budget.amount_micros = int(round(amount * 1_000_000))
        client.copy_from(
            op.update_mask, protobuf_helpers.field_mask(None, budget._pb)
        )

        request = client.get_type("MutateCampaignBudgetsRequest")
        request.customer_id = cid
        request.operations.append(op)
        request.validate_only = bool(dry_run)

        response = svc.mutate_campaign_budgets(request=request)
        result = {
            "campaign": c.get("name"),
            "previous_daily_budget": micros_to_unit(b.get("amount_micros")),
            "new_daily_budget": round(amount, 2),
        }
        if dry_run:
            result.update(dry_run=True, valid=True)
        else:
            result.update(updated=response.results[0].resource_name)
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def add_negative_keywords(
    campaign_id: str,
    keywords: list[str],
    match_type: str = "EXACT",
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Add negative keywords to a campaign. WRITE TOOL - confirm with the user first.

    Args:
        campaign_id: numeric campaign ID.
        keywords: list of keyword texts to exclude.
        match_type: "EXACT", "PHRASE" or "BROAD" (default EXACT).
        dry_run: if true, validates without applying.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        match_type = match_type.strip().upper()
        if match_type not in ("EXACT", "PHRASE", "BROAD"):
            return fail("match_type doit etre EXACT, PHRASE ou BROAD.")
        if not keywords:
            return fail("Fournir au moins un mot-cle.")
        client = get_client()
        cid = resolve_cid(customer_id)

        campaign_svc = client.get_service("CampaignService")
        svc = client.get_service("CampaignCriterionService")
        request = client.get_type("MutateCampaignCriteriaRequest")
        request.customer_id = cid
        request.validate_only = bool(dry_run)

        for text in keywords:
            text = str(text).strip()
            if not text:
                continue
            op = client.get_type("CampaignCriterionOperation")
            crit = op.create
            crit.campaign = campaign_svc.campaign_path(cid, str(int(campaign_id)))
            crit.negative = True
            crit.keyword.text = text
            crit.keyword.match_type = getattr(
                client.enums.KeywordMatchTypeEnum, match_type
            )
            request.operations.append(op)

        response = svc.mutate_campaign_criteria(request=request)
        if dry_run:
            return ok(
                {
                    "dry_run": True,
                    "valid": True,
                    "would_add": len(request.operations),
                    "match_type": match_type,
                }
            )
        return ok(
            {
                "added": len(response.results),
                "match_type": match_type,
                "keywords": [str(k).strip() for k in keywords if str(k).strip()],
            }
        )
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def set_campaign_target_roas(
    campaign_id: str,
    target_roas: Optional[float] = None,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Set or clear the target ROAS of a "Maximize conversion value" campaign.
    WRITE TOOL - confirm with the user first.

    Only for campaigns whose bidding strategy is MAXIMIZE_CONVERSION_VALUE
    (standard strategy, not a portfolio); other strategies are refused.

    Args:
        campaign_id: numeric campaign ID.
        target_roas: ratio, not a percentage: 3.0 = 300 % return on ad spend.
            None or 0 removes the target (pure "maximize conversion value").
        dry_run: if true, validates the change without applying it.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        new_target = None if target_roas in (None, 0, 0.0) else float(target_roas)
        if new_target is not None and new_target < 0:
            return fail(
                "target_roas doit etre positif (ou 0/None pour retirer la cible)."
            )
        if new_target is not None and new_target >= 100:
            return fail(
                "target_roas est un ratio, pas un pourcentage : 3.0 = 300 %. "
                f"Valeur recue : {new_target}."
            )
        client = get_client()
        cid = resolve_cid(customer_id)

        row = campaign_row(
            cid,
            campaign_id,
            "campaign.id, campaign.name, campaign.bidding_strategy_type, "
            "campaign.bidding_strategy, campaign.maximize_conversion_value.target_roas",
        )
        if row is None:
            return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")
        c = row.get("campaign", {})
        if c.get("bidding_strategy_type") != "MAXIMIZE_CONVERSION_VALUE":
            return fail(
                {
                    "message": (
                        "Cette campagne n'utilise pas la strategie "
                        "MAXIMIZE_CONVERSION_VALUE ; un ROAS cible ne peut etre "
                        "defini que sur cette strategie."
                    ),
                    "campaign": c.get("name"),
                    "bidding_strategy": c.get("bidding_strategy_type"),
                }
            )
        if c.get("bidding_strategy"):
            return fail(
                {
                    "message": (
                        "Cette campagne utilise une strategie d'encheres de "
                        "portefeuille ; modifier la cible sur la strategie elle-meme "
                        "(ressource bidding_strategy), pas sur la campagne."
                    ),
                    "campaign": c.get("name"),
                    "bidding_strategy": c.get("bidding_strategy"),
                }
            )
        previous = c.get("maximize_conversion_value", {}).get("target_roas")

        svc = client.get_service("CampaignService")
        request = client.get_type("MutateCampaignsRequest")
        request.customer_id = cid
        request.operations.append(
            _build_target_roas_op(client, cid, campaign_id, new_target)
        )
        request.validate_only = bool(dry_run)

        response = svc.mutate_campaigns(request=request)
        result: dict[str, Any] = {
            "campaign": c.get("name"),
            "previous_target_roas": previous,
            "new_target_roas": new_target,
            "note": "Ratio : 3.0 = 300 % de retour sur depenses publicitaires.",
        }
        if dry_run:
            result.update(dry_run=True, valid=True)
        else:
            result.update(updated=response.results[0].resource_name)
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def set_campaign_conversion_goals(
    campaign_id: str,
    biddable_categories: list[str],
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Give a campaign its own conversion goals and choose which are biddable.
    WRITE TOOL - confirm with the user first.

    Switches the campaign to campaign-specific goals (goal_config_level =
    CAMPAIGN) and then, for every campaign_conversion_goal of the campaign,
    sets biddable = (category in biddable_categories). Use
    get_campaign_conversion_goals first to see the existing categories.

    Args:
        campaign_id: numeric campaign ID.
        biddable_categories: ConversionActionCategory names to optimize for,
            e.g. ["PURCHASE"] or ["PURCHASE", "BEGIN_CHECKOUT"]. Other
            categories present in the campaign (ADD_TO_CART, PAGE_VIEW,
            SUBMIT_LEAD_FORM, CONTACT, SIGNUP, DEFAULT, ...) become
            non-biddable (observation only). At least one is required.
        dry_run: if true, validates without applying.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        client = get_client()
        cid = resolve_cid(customer_id)

        valid_categories = [
            v.name
            for v in client.enums.ConversionActionCategoryEnum
            if v.name not in ("UNSPECIFIED", "UNKNOWN")
        ]
        wanted: list[str] = []
        for cat in biddable_categories or []:
            name = str(cat).strip().upper()
            if name not in valid_categories:
                return fail(
                    {
                        "message": f"Categorie inconnue : {cat!r}.",
                        "valid_categories": valid_categories,
                    }
                )
            if name not in wanted:
                wanted.append(name)
        if not wanted:
            return fail("Fournir au moins une categorie d'objectif (ex. PURCHASE).")

        c, cfg, goals = _conversion_goal_state(cid, campaign_id)
        if c is None:
            return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")
        current_level = cfg.get("goal_config_level")
        if not goals:
            return fail(
                {
                    "message": (
                        "Aucun objectif de conversion (campaign_conversion_goal) "
                        "n'existe pour cette campagne : le compte n'a probablement "
                        "aucune action de conversion configuree."
                    ),
                    "campaign": c.get("name"),
                }
            )
        present = sorted({g["category"] for g in goals})
        missing = [cat for cat in wanted if cat not in present]
        if missing:
            return fail(
                {
                    "message": (
                        "Categories sans objectif de conversion dans cette campagne "
                        f"(aucune action de conversion de ce type) : {missing}."
                    ),
                    "available_categories": present,
                }
            )

        # 1) Objectifs propres a la campagne (requete separee).
        level_changed = False
        if current_level != "CAMPAIGN":
            cfg_svc = client.get_service("ConversionGoalCampaignConfigService")
            cfg_request = client.get_type("MutateConversionGoalCampaignConfigsRequest")
            cfg_request.customer_id = cid
            cfg_request.operations.append(
                _build_goal_config_op(client, cid, campaign_id)
            )
            cfg_request.validate_only = bool(dry_run)
            cfg_svc.mutate_conversion_goal_campaign_configs(request=cfg_request)
            level_changed = True

        # 2) Valeur "biddable" de chaque objectif (une requete, une operation
        #    par objectif dont la valeur change).
        ops, changes = _build_conversion_goal_ops(
            client, cid, campaign_id, goals, wanted
        )
        updated = 0
        if ops:
            goal_svc = client.get_service("CampaignConversionGoalService")
            goal_request = client.get_type("MutateCampaignConversionGoalsRequest")
            goal_request.customer_id = cid
            for op in ops:
                goal_request.operations.append(op)
            goal_request.validate_only = bool(dry_run)
            response = goal_svc.mutate_campaign_conversion_goals(request=goal_request)
            updated = len(response.results)

        biddable_map = {cat: cat in wanted for cat in present}
        result: dict[str, Any] = {
            "campaign": c.get("name"),
            "goal_config_level": "CAMPAIGN",
            "goal_config_level_changed": level_changed,
            "biddable": biddable_map,
            "changed_goals": changes,
            "unchanged_goals": len(goals) - len(changes),
        }
        if dry_run:
            result.update(dry_run=True, valid=True)
        else:
            result.update(updated_goals=updated)
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def add_search_themes(
    asset_group_id: str,
    themes: list[str],
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Add search themes to a Performance Max asset group. WRITE TOOL - confirm
    with the user first.

    Search themes (asset_group_signal.search_theme) tell Performance Max which
    searches are relevant. Google allows at most 25 per asset group; themes
    already present are skipped.

    Args:
        asset_group_id: numeric asset group ID (from list_asset_groups).
        themes: list of search theme texts (a few words each).
        dry_run: if true, validates without applying.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        cleaned: list[str] = []
        for t in themes or []:
            text = str(t).strip()
            if text and text.lower() not in (x.lower() for x in cleaned):
                cleaned.append(text)
        if not cleaned:
            return fail("Fournir au moins un theme de recherche.")
        client = get_client()
        cid = resolve_cid(customer_id)
        ag_id = int(asset_group_id)

        rows = run_query(
            cid,
            "SELECT asset_group.id, asset_group.name, campaign.name, "
            "campaign.advertising_channel_type FROM asset_group "
            f"WHERE asset_group.id = {ag_id}",
            limit=1,
        )
        if not rows:
            return fail(
                f"Groupe d'assets {asset_group_id} introuvable dans le compte {cid}."
            )
        group_name = rows[0].get("asset_group", {}).get("name")
        existing = [
            r.get("asset_group_signal", {}).get("search_theme", {}).get("text", "")
            for r in run_query(
                cid,
                "SELECT asset_group_signal.search_theme.text FROM asset_group_signal "
                f"WHERE asset_group.id = {ag_id}",
                limit=200,
            )
        ]
        existing = [t for t in existing if t]
        existing_lower = {t.lower() for t in existing}
        new_themes = [t for t in cleaned if t.lower() not in existing_lower]
        skipped = [t for t in cleaned if t.lower() in existing_lower]
        if not new_themes:
            return ok(
                {
                    "asset_group": group_name,
                    "added": 0,
                    "already_present": skipped,
                    "existing_count": len(existing),
                }
            )
        if len(existing) + len(new_themes) > SEARCH_THEMES_MAX:
            return fail(
                {
                    "message": (
                        f"Maximum {SEARCH_THEMES_MAX} themes de recherche par "
                        f"groupe d'assets : {len(existing)} existants + "
                        f"{len(new_themes)} nouveaux."
                    ),
                    "existing": existing,
                }
            )

        svc = client.get_service("AssetGroupSignalService")
        request = client.get_type("MutateAssetGroupSignalsRequest")
        request.customer_id = cid
        request.validate_only = bool(dry_run)
        for op in _build_search_theme_ops(client, cid, str(ag_id), new_themes):
            request.operations.append(op)

        response = svc.mutate_asset_group_signals(request=request)
        result: dict[str, Any] = {
            "asset_group": group_name,
            "themes": new_themes,
            "already_present": skipped,
            "total_after": len(existing) + len(new_themes),
        }
        if dry_run:
            result.update(dry_run=True, valid=True, would_add=len(new_themes))
        else:
            result.update(
                added=len(response.results),
                resource_names=[r.resource_name for r in response.results],
            )
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def set_campaign_url_expansion(
    campaign_id: str,
    opt_out: bool,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Opt a Performance Max (or Search) campaign out of / into final URL
    expansion. WRITE TOOL - confirm with the user first.

    opt_out=true: ads only send traffic to the final URLs of the asset groups
    (or feed URLs), no automatically chosen landing pages. opt_out=false:
    Google may pick other pages of the site and generate matching text assets.
    Since Google Ads API v22 this is the asset automation setting
    FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION (OPTED_OUT / OPTED_IN), which
    replaced campaign.url_expansion_opt_out. Other asset automation settings
    of the campaign are preserved.

    Args:
        campaign_id: numeric campaign ID.
        opt_out: true to disable URL expansion, false to enable it.
        dry_run: if true, validates without applying.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        client = get_client()
        cid = resolve_cid(customer_id)

        row = campaign_row(
            cid,
            campaign_id,
            "campaign.id, campaign.name, campaign.advertising_channel_type, "
            "campaign.asset_automation_settings",
        )
        if row is None:
            return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")
        c = row.get("campaign", {})
        channel = c.get("advertising_channel_type")
        if channel not in ("PERFORMANCE_MAX", "SEARCH"):
            return fail(
                {
                    "message": (
                        "L'expansion d'URL finale ne concerne que les campagnes "
                        "Performance Max (et Search)."
                    ),
                    "campaign": c.get("name"),
                    "channel_type": channel,
                }
            )
        new_status = "OPTED_OUT" if opt_out else "OPTED_IN"
        settings, previous_status = merge_automation_settings(
            c.get("asset_automation_settings", []),
            FINAL_URL_EXPANSION_SETTING,
            new_status,
        )

        svc = client.get_service("CampaignService")
        request = client.get_type("MutateCampaignsRequest")
        request.customer_id = cid
        request.operations.append(
            _build_asset_automation_op(client, cid, campaign_id, settings)
        )
        request.validate_only = bool(dry_run)

        response = svc.mutate_campaigns(request=request)
        result: dict[str, Any] = {
            "campaign": c.get("name"),
            "url_expansion_opt_out": bool(opt_out),
            "previous_setting": previous_status or "(defaut Google)",
            "new_setting": new_status,
            "asset_automation_settings": [
                {"type": t, "status": st} for t, st in settings
            ],
        }
        if dry_run:
            result.update(dry_run=True, valid=True)
        else:
            result.update(updated=response.results[0].resource_name)
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def create_search_campaign(
    spec: dict, dry_run: bool = False, customer_id: Optional[str] = None
) -> str:
    """Create a complete Search campaign in ONE atomic request. WRITE TOOL -
    confirm with the user first (show them the spec and use dry_run=true).

    Creates: a dedicated daily budget, the campaign (Google Search only, no
    search partners / display network, location targeting by physical
    presence, EU political advertising declared as "does not contain"),
    location and language targeting, campaign-level negative keywords, the
    ad groups with one responsive search ad and their keywords each, and
    sitelink/callout assets linked to the campaign. Everything is created
    together or not at all (temporary resource IDs, no partial failure).

    Args:
        spec: dict with keys:
          name: campaign name (must be unique in the account).
          daily_budget: daily amount in the ACCOUNT CURRENCY (e.g. 25.0).
          status: "PAUSED" (default, recommended) or "ENABLED".
          bidding: {"type": "MAXIMIZE_CONVERSIONS" (default) |
            "MAXIMIZE_CONVERSION_VALUE" | "TARGET_CPA",
            "target_cpa": float optional (account currency; required for
            TARGET_CPA, which is created as "maximize conversions with a
            target CPA"), "target_roas": float optional, ratio 3.0 = 300 %,
            only with MAXIMIZE_CONVERSION_VALUE}.
          geo_target_constant_ids: list of ints, required (20123 = Quebec,
            2124 = Canada). Use run_gaql on geo_target_constant to look
            other ids up (SELECT geo_target_constant.id,
            geo_target_constant.name, geo_target_constant.canonical_name
            FROM geo_target_constant WHERE geo_target_constant.name = '...').
          language_constant_ids: list of ints (1002 = French, 1000 = English).
          negative_keywords: list of {"text", "match_type"} (campaign level).
          ad_groups: list of ad group specs (at least one), each a dict:
            name: ad group name (unique within the campaign).
            final_url: landing page, https://...
            keywords: list of {"text", "match_type": "EXACT"|"PHRASE"|"BROAD"}
              (at least one).
            headlines: 3 to 15 texts, each 30 characters max.
            descriptions: 2 to 4 texts, each 90 characters max.
            path1, path2: optional display path segments, 15 characters max.
          sitelinks: list of {"text" (25 max), "final_url", "description1",
            "description2" (35 max each, both or none)}.
          callouts: list of texts, 25 characters max each.
        dry_run: if true, the whole request is validated by Google without
            creating anything; the result lists the operations by type.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        if (
            isinstance(spec, dict)
            and str(spec.get("advertising_channel_type") or "").strip().upper()
            == "PERFORMANCE_MAX"
        ):
            # Passerelle : une spec Performance Max est traitee par
            # create_pmax_campaign (meme format de spec, meme dry_run).
            return create_pmax_campaign(spec, dry_run=dry_run, customer_id=customer_id)
        try:
            normalized = validate_search_campaign_spec(spec)
        except SpecError as ex:
            return fail(str(ex))
        client = get_client()
        cid = resolve_cid(customer_id)

        ops = _build_search_campaign_ops(client, cid, normalized)
        response = mutate_atomic(client, cid, ops, dry_run)
        summary = {
            "campaign_name": normalized["name"],
            "status": normalized["status"],
            "daily_budget": round(normalized["daily_budget"], 2),
            "bidding": normalized["bidding"],
            "ad_groups": [ag["name"] for ag in normalized["ad_groups"]],
            "operations": count_ops(ops),
        }
        if dry_run:
            summary.update(dry_run=True, valid=True)
            return ok(summary)
        created = created_resource_names(response)
        campaign_rns = created.get("campaign", [])
        summary.update(
            created=created,
            campaign_id=campaign_rns[0].split("/")[-1] if campaign_rns else None,
            note=(
                "Campagne creee en PAUSE : verifier dans l'interface puis activer "
                "avec set_campaign_status."
                if normalized["status"] == "PAUSED"
                else "Campagne creee ACTIVE : elle peut diffuser des maintenant."
            ),
        )
        return ok(summary)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def create_pmax_campaign(
    spec: dict, dry_run: bool = False, customer_id: Optional[str] = None
) -> str:
    """Create a complete Performance Max campaign (retail, with a Merchant
    Center feed) in ONE atomic request. WRITE TOOL - confirm with the user
    first (show them the spec and use dry_run=true).

    Creates: a dedicated daily budget, the campaign (Maximize conversion value,
    optional target ROAS, brand guidelines enabled, final URL expansion opted
    out by default, EU political advertising declared as "does not contain"),
    the business name and logo campaign assets, location / language targeting,
    campaign-level negative keywords, ONE asset group with new text assets
    (headlines, long headlines, descriptions) and links to EXISTING image /
    video assets of the account, a listing group filter (all products, or only
    the given Merchant Center item ids), search themes and audience signals,
    plus optional sitelinks / callouts. Everything is created together or not
    at all. Conversion goals are NOT changed here: call
    set_campaign_conversion_goals afterwards if the campaign must bid on
    purchases only.

    Args:
        spec: dict with keys:
          name: campaign name (unique in the account).
          daily_budget: daily amount in the ACCOUNT CURRENCY.
          status: "PAUSED" (default, recommended) or "ENABLED".
          target_roas: optional ratio (4.0 = 400 %); omit for no target.
          geo_target_constant_ids: list of ints, required (2124 = Canada,
            20123 = Quebec). geo_target_type: "PRESENCE" (default) or
            "PRESENCE_OR_INTEREST".
          language_constant_ids: list of ints (1000 = English, 1002 = French).
          negative_keywords: list of {"text", "match_type"} (campaign level).
          final_url: landing page of the asset group. asset_group_name,
            path1, path2: optional.
          headlines: 3-15 texts (30 chars max). long_headlines: 1-5 texts
            (90 max). descriptions: 2-5 texts (90 max, at least one <= 60).
          business_name (25 max) or business_name_asset_id (existing text
            asset). logo_asset_ids (>= 1 square logo, >= 128 px),
            landscape_logo_asset_ids, marketing_image_asset_ids (>= 1),
            square_marketing_image_asset_ids (>= 1),
            portrait_marketing_image_asset_ids, youtube_video_asset_ids:
            IDs of EXISTING assets (run_gaql on asset / asset_group_asset).
          merchant_id, feed_label, enable_local: Merchant Center settings.
          listing_group: {"include_item_ids": [...]} to advertise only those
            Merchant Center item ids (everything else excluded); omit to
            advertise the whole feed.
          search_themes: up to 25 texts. audience_ids: existing audience ids.
          url_expansion_opt_out: bool, default true.
          sitelinks / callouts: same format as create_search_campaign.
          clone_from_campaign_id: optional existing Performance Max campaign;
            every field above that is omitted (merchant settings, business
            name, logos, images, videos, item ids, audiences) is copied from
            it. Text assets and search themes are never copied: supply them
            in the target language.
        dry_run: if true, the whole request is validated by Google without
            creating anything; the result lists the operations by type.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        try:
            normalized = validate_pmax_campaign_spec(spec)
        except SpecError as ex:
            return fail(str(ex))
        client = get_client()
        cid = resolve_cid(customer_id)
        try:
            source = (
                resolve_pmax_clone_source(cid, normalized["clone_from_campaign_id"])
                if normalized["clone_from_campaign_id"]
                else None
            )
            final = finalize_pmax_spec(normalized, source)
        except SpecError as ex:
            return fail(str(ex))

        ops = _build_pmax_campaign_ops(client, cid, final)
        response = mutate_atomic(client, cid, ops, dry_run)
        summary: dict[str, Any] = {
            "campaign_name": final["name"],
            "status": final["status"],
            "daily_budget": round(final["daily_budget"], 2),
            "target_roas": final["target_roas"],
            "geo_target_constant_ids": final["geo_target_constant_ids"],
            "language_constant_ids": final["language_constant_ids"],
            "merchant_id": final["merchant_id"],
            "feed_label": final["feed_label"],
            "final_url": final["final_url"],
            "texts": {
                "headlines": len(final["headlines"]),
                "long_headlines": len(final["long_headlines"]),
                "descriptions": len(final["descriptions"]),
            },
            "reused_assets": {
                PMAX_ASSET_KEYS[ft]: len(ids) for ft, ids in final["assets"].items() if ids
            },
            "business_name": final["business_name"] or final["cloned"].get("business_name"),
            "listing_group": (
                f"{len(final['include_item_ids'])} fiches incluses, le reste exclu"
                if final["include_item_ids"] is not None
                else "tout le flux"
            ),
            "search_themes": len(final["search_themes"]),
            "audience_ids": final["audience_ids"],
            "negative_keywords": len(final["negative_keywords"]),
            "url_expansion_opt_out": final["url_expansion_opt_out"],
            "cloned_from_source": final["cloned"],
            "operations": count_ops(ops),
        }
        if final.get("source_campaign"):
            summary["source_campaign"] = final["source_campaign"]
        if dry_run:
            summary.update(dry_run=True, valid=True)
            return ok(summary)
        created = created_resource_names(response)
        campaign_rns = created.get("campaign", [])
        group_rns = created.get("asset_group", [])
        summary.update(
            created={k: len(v) for k, v in created.items()},
            campaign_id=campaign_rns[0].split("/")[-1] if campaign_rns else None,
            asset_group_id=group_rns[0].split("/")[-1] if group_rns else None,
        )
        # Seconde requete : le groupe d'assets, cree en PAUSE (voir
        # _build_pmax_campaign_ops), est active maintenant que ses assets existent.
        group_status = "PAUSED"
        if group_rns:
            try:
                request = client.get_type("MutateAssetGroupsRequest")
                request.customer_id = cid
                request.operations.append(
                    _build_asset_group_status_op(client, group_rns[0], "ENABLED")
                )
                client.get_service("AssetGroupService").mutate_asset_groups(
                    request=request
                )
                group_status = "ENABLED"
            except Exception as ex:  # la campagne existe : on le dit clairement
                summary["asset_group_activation_error"] = json.loads(
                    format_google_ads_error(ex)
                ).get("error")
        summary["asset_group_status"] = group_status
        summary["note"] = (
            (
                "Campagne creee en PAUSE : verifier dans l'interface puis activer "
                "avec set_campaign_status."
                if final["status"] == "PAUSED"
                else "Campagne creee ACTIVE : elle peut diffuser des maintenant."
            )
            + (
                ""
                if group_status == "ENABLED"
                else " ATTENTION : le groupe d'assets est reste en PAUSE, l'activer "
                "dans l'interface Google Ads."
            )
            + " Objectifs de conversion : set_campaign_conversion_goals si besoin."
        )
        return ok(summary)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def add_search_ad_group(
    campaign_id: str,
    spec: dict,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Add an ad group (with one responsive search ad and its keywords) to an
    existing Search campaign, in one atomic request. WRITE TOOL - confirm with
    the user first.

    Args:
        campaign_id: numeric ID of an existing SEARCH campaign.
        spec: dict with keys:
          name: ad group name (unique within the campaign).
          final_url: landing page, https://...
          keywords: list of {"text", "match_type": "EXACT"|"PHRASE"|"BROAD"}
            (at least one).
          headlines: 3 to 15 texts, each 30 characters max.
          descriptions: 2 to 4 texts, each 90 characters max.
          path1, path2: optional display path segments, 15 characters max.
        dry_run: if true, validates without creating anything.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        try:
            normalized = validate_ad_group_spec(spec)
        except SpecError as ex:
            return fail(str(ex))
        client = get_client()
        cid = resolve_cid(customer_id)

        row = campaign_row(
            cid,
            campaign_id,
            "campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type",
        )
        if row is None:
            return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")
        c = row.get("campaign", {})
        if c.get("advertising_channel_type") != "SEARCH":
            return fail(
                {
                    "message": "Cet outil ne s'applique qu'aux campagnes SEARCH.",
                    "campaign": c.get("name"),
                    "channel_type": c.get("advertising_channel_type"),
                }
            )
        campaign_rn = client.get_service("CampaignService").campaign_path(
            cid, str(int(campaign_id))
        )
        ops = _build_ad_group_ops(
            client, cid, campaign_rn, normalized, itertools.count(-1, -1)
        )
        response = mutate_atomic(client, cid, ops, dry_run)
        summary: dict[str, Any] = {
            "campaign": c.get("name"),
            "campaign_status": c.get("status"),
            "ad_group": normalized["name"],
            "keywords": len(normalized["keywords"]),
            "operations": count_ops(ops),
        }
        if dry_run:
            summary.update(dry_run=True, valid=True)
        else:
            summary.update(created=created_resource_names(response))
        return ok(summary)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def add_campaign_assets(
    campaign_id: str,
    sitelinks: Optional[list[dict]] = None,
    callouts: Optional[list[str]] = None,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Create sitelink and/or callout assets and link them to an existing
    campaign, in one atomic request. WRITE TOOL - confirm with the user first.

    Args:
        campaign_id: numeric campaign ID.
        sitelinks: list of {"text" (25 chars max), "final_url",
            "description1", "description2" (35 chars max each, both or none)}.
        callouts: list of short texts (25 characters max each).
        dry_run: if true, validates without creating anything.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        try:
            links = validate_sitelinks(sitelinks)
            texts = validate_callouts(callouts)
        except SpecError as ex:
            return fail(str(ex))
        if not links and not texts:
            return fail("Fournir au moins un sitelink ou un callout.")
        client = get_client()
        cid = resolve_cid(customer_id)

        row = campaign_row(
            cid,
            campaign_id,
            "campaign.id, campaign.name, campaign.advertising_channel_type",
        )
        if row is None:
            return fail(f"Campagne {campaign_id} introuvable dans le compte {cid}.")
        c = row.get("campaign", {})
        campaign_rn = client.get_service("CampaignService").campaign_path(
            cid, str(int(campaign_id))
        )
        ops = _build_campaign_asset_ops(
            client, cid, campaign_rn, links, texts, itertools.count(-1, -1)
        )
        response = mutate_atomic(client, cid, ops, dry_run)
        summary: dict[str, Any] = {
            "campaign": c.get("name"),
            "sitelinks": [s["text"] for s in links],
            "callouts": texts,
            "operations": count_ops(ops),
        }
        if dry_run:
            summary.update(dry_run=True, valid=True)
        else:
            summary.update(created=created_resource_names(response))
        return ok(summary)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def create_customer_match_list(
    name: str,
    description: str = "",
    membership_life_span_days: int = 540,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Create an empty Customer Match user list (CRM-based, contact info:
    emails, phone numbers, names + postal addresses). WRITE TOOL - confirm with
    the user first.

    The list is created OPEN, with first-party data and upload key type
    CONTACT_INFO; members are added afterwards with
    upload_customer_match_members. A list with the same name (exact or
    case-insensitive) is never duplicated: the tool refuses and returns the
    existing list's resource name and id instead.

    Args:
        name: list name (unique in the account).
        description: optional description.
        membership_life_span_days: days a member stays in the list after its
            most recent upload, 1 to 540. 540 days is Google's current maximum
            for Customer Match lists ("no expiration" is no longer accepted);
            larger values are capped to 540.
        dry_run: if true, validates with Google (validate_only) without
            creating the list.
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        list_name = str(name or "").strip()
        if not list_name:
            return fail("name : nom de liste requis.")
        try:
            days = int(membership_life_span_days)
        except (TypeError, ValueError):
            return fail("membership_life_span_days : entier attendu (1 a 540).")
        if days < 1:
            return fail(
                "membership_life_span_days doit etre >= 1 "
                f"(maximum {CUSTOMER_MATCH_MAX_LIFE_SPAN_DAYS})."
            )
        capped = days > CUSTOMER_MATCH_MAX_LIFE_SPAN_DAYS
        days = min(days, CUSTOMER_MATCH_MAX_LIFE_SPAN_DAYS)
        client = get_client()
        cid = resolve_cid(customer_id)

        existing = _find_user_list_by_name(cid, list_name)
        if existing:
            return fail(
                {
                    "message": (
                        f"Une liste nommee {existing.get('name')!r} existe deja : "
                        "aucune liste creee. Utiliser son id avec "
                        "upload_customer_match_members, ou choisir un autre nom."
                    ),
                    "existing_resource_name": existing.get("resource_name"),
                    "existing_id": str(existing.get("id")),
                    "existing_type": existing.get("type"),
                    "existing_upload_key_type": existing.get(
                        "crm_based_user_list", {}
                    ).get("upload_key_type"),
                    "existing_membership_status": existing.get("membership_status"),
                }
            )

        svc = client.get_service("UserListService")
        request = client.get_type("MutateUserListsRequest")
        request.customer_id = cid
        request.operations.append(
            _build_user_list_op(client, list_name, str(description or "").strip(), days)
        )
        request.validate_only = bool(dry_run)

        response = svc.mutate_user_lists(request=request)
        result: dict[str, Any] = {
            "name": list_name,
            "type": "CRM_BASED",
            "upload_key_type": "CONTACT_INFO",
            "data_source_type": "FIRST_PARTY",
            "membership_status": "OPEN",
            "membership_life_span_days": days,
        }
        if capped:
            result["life_span_note"] = (
                f"{membership_life_span_days} jours demandes, ramenes a "
                f"{CUSTOMER_MATCH_MAX_LIFE_SPAN_DAYS} (maximum Google pour Customer Match)."
            )
        if dry_run:
            result.update(dry_run=True, valid=True)
        else:
            rn = response.results[0].resource_name
            result.update(
                resource_name=rn,
                id=rn.split("/")[-1],
                note=(
                    "Liste creee vide : ajouter des membres avec "
                    "upload_customer_match_members (user_list_id = id)."
                ),
            )
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


@write_tool
def upload_customer_match_members(
    user_list_id: str,
    members: list[dict],
    remove: bool = False,
    dry_run: bool = False,
    customer_id: Optional[str] = None,
) -> str:
    """Add members to (or remove them from) a Customer Match user list from raw
    contact data: emails, phone numbers, names + postal addresses. WRITE TOOL -
    confirm with the user first.

    Personal data never reaches Google in clear text: each value is normalised
    and SHA-256 hashed on this server following Google's Customer Match rules
    (email: trimmed, lowercased, dots removed from the local part of gmail.com
    / googlemail.com addresses; phone: E.164 with leading "+", a 10-digit
    North American number gets +1; first/last name: trimmed and lowercased,
    accents kept). Country codes (2-letter ISO) and postal codes (trimmed,
    uppercase) are sent as is. One UserData per member with one identifier
    per available key: hashed_email, hashed_phone_number and address_info
    (only when first_name, last_name, country_code AND postal_code are all
    present). Members without any usable identifier are skipped and counted;
    exact duplicates are sent once. The upload is an OfflineUserDataJob of
    type CUSTOMER_MATCH_USER_LIST (consent ad_user_data / ad_personalization
    declared GRANTED: by uploading, the user confirms these people consented),
    operations sent in batches of 1000 with partial failure enabled, then run.

    Args:
        user_list_id: numeric ID of a CRM_BASED list with upload key
            CONTACT_INFO (from create_customer_match_list or list_user_lists).
        members: list of objects, one per person, with any of the keys email,
            phone, first_name, last_name, country_code, postal_code (the
            Google template headers Email, Phone, First Name, Last Name,
            Country, Zip are accepted too). Up to 5000 members per call.
        remove: false (default) adds the members, true removes them.
        dry_run: if true, everything is normalised, hashed, validated and
            counted locally and the counts are returned WITHOUT any call to
            Google (no job is created).
        customer_id: optional 10-digit account ID; defaults to the server's account.
    """
    try:
        require_writes()
        try:
            prepared = prepare_customer_match_members(members)
        except SpecError as ex:
            return fail(str(ex))
        counts = prepared["counts"]
        result: dict[str, Any] = {
            "user_list_id": str(user_list_id),
            "operation": "remove" if remove else "add",
            "counts": counts,
            "skipped_member_positions": prepared["skipped_positions"],
            "hashing": (
                "Normalisation + SHA-256 faits sur le serveur ; seules les "
                "empreintes (et pays / code postal) sont envoyees a Google."
            ),
        }
        if dry_run:
            result.update(
                dry_run=True,
                api_called=False,
                valid=bool(prepared["members"]),
                would_send_requests=counts["requests_needed"],
            )
            return ok(result)
        if not prepared["members"]:
            return fail(
                {
                    "message": (
                        "Aucun membre exploitable : il faut au moins un email, un "
                        "telephone ou une adresse complete (prenom, nom, pays, "
                        "code postal) par membre."
                    ),
                    "counts": counts,
                    "skipped_member_positions": prepared["skipped_positions"],
                }
            )
        client = get_client()
        cid = resolve_cid(customer_id)
        ul_id = int(user_list_id)

        rows = run_query(
            cid,
            f"SELECT {USER_LIST_FIELDS} FROM user_list WHERE user_list.id = {ul_id}",
            limit=1,
        )
        if not rows:
            return fail(f"Liste d'audience {user_list_id} introuvable dans le compte {cid}.")
        u = rows[0].get("user_list", {})
        list_type = u.get("type")
        key_type = u.get("crm_based_user_list", {}).get("upload_key_type")
        if list_type not in (None, "CRM_BASED") or key_type not in (None, "CONTACT_INFO"):
            return fail(
                {
                    "message": (
                        "Cette liste n'est pas une liste Customer Match a "
                        "coordonnees (type CRM_BASED, cle CONTACT_INFO) : "
                        "impossible d'y importer des emails / telephones / "
                        "adresses. Creer une liste avec create_customer_match_list."
                    ),
                    "user_list": user_list_summary(u),
                }
            )
        if u.get("membership_status") == "CLOSED" and not remove:
            return fail(
                {
                    "message": (
                        "Cette liste est fermee (membership_status = CLOSED) : "
                        "elle n'accepte plus de nouveaux membres. La rouvrir dans "
                        "l'interface Google Ads (Audiences) avant l'import."
                    ),
                    "user_list": user_list_summary(u),
                }
            )
        user_list_rn = u.get("resource_name") or client.get_service(
            "UserListService"
        ).user_list_path(cid, str(ul_id))

        svc = client.get_service("OfflineUserDataJobService")
        create_request = client.get_type("CreateOfflineUserDataJobRequest")
        create_request.customer_id = cid
        create_request.job = _build_offline_user_data_job(client, user_list_rn)
        create_request.enable_match_rate_range_preview = True
        job_rn = svc.create_offline_user_data_job(request=create_request).resource_name

        requests_sent = 0
        partial_failures: list[dict] = []
        for request in _build_add_operations_requests(
            client, job_rn, prepared["members"], remove=remove
        ):
            response = svc.add_offline_user_data_job_operations(request=request)
            requests_sent += 1
            failure = summarize_partial_failure(
                client, getattr(response, "partial_failure_error", None)
            )
            if failure:
                failure["request_index"] = requests_sent
                partial_failures.append(failure)

        rejected = sum(f["failed_operations"] for f in partial_failures)
        if rejected >= counts["members_to_upload"]:
            # Tout a ete rejete : inutile de lancer le job, on remonte le detail.
            return fail(
                {
                    "message": (
                        "Google a rejete toutes les operations (erreurs partielles) ; "
                        "le job n'a pas ete lance."
                    ),
                    "job_resource_name": job_rn,
                    "partial_failures": partial_failures,
                    "counts": counts,
                }
            )

        run_request = client.get_type("RunOfflineUserDataJobRequest")
        run_request.resource_name = job_rn
        operation = svc.run_offline_user_data_job(request=run_request)

        result.update(
            job_resource_name=job_rn,
            job_id=job_rn.split("/")[-1],
            job_status="PENDING",
            user_list={
                "resource_name": user_list_rn,
                "id": str(ul_id),
                "name": u.get("name"),
            },
            requests_sent=requests_sent,
            operations_sent=counts["members_to_upload"],
            operations_rejected=rejected,
            partial_failures=partial_failures,
            long_running_operation=getattr(
                getattr(operation, "operation", None), "name", None
            ),
            note=CUSTOMER_MATCH_PROCESSING_NOTE,
        )
        return ok(result)
    except Exception as ex:
        return format_google_ads_error(ex)


# ---------------------------------------------------------------------------
# Application ASGI : chemin secret + endpoint de sante
# ---------------------------------------------------------------------------

_inner_app = mcp.streamable_http_app()  # sert le protocole MCP sur /mcp


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def find_key(secret: str) -> Optional[dict]:
    """Cle d'acces correspondant a `secret` (comparaison a temps constant)."""
    candidate = secret.encode("utf-8")
    found = None
    for known, user in AUTH_KEYS.items():
        if hmac.compare_digest(candidate, known.encode("utf-8")):
            found = user
    return found


def _bearer_user(scope) -> Optional[dict]:
    """Cle d'acces de l'en-tete Authorization: Bearer <secret>, ou None."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            try:
                token = value.decode().strip()
            except UnicodeDecodeError:
                return None
            if token.lower().startswith("bearer "):
                return find_key(token[7:].strip())
    return None


def health_payload() -> dict:
    roles = [user["role"] for user in AUTH_KEYS.values()]
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "brand": BRAND or None,
        "version": VERSION,
        "writes_enabled": ALLOW_WRITES,
        "google_ads_configured": all(os.environ.get(v) for v in REQUIRED_GOOGLE_VARS),
        "default_customer_configured": bool(SERVED_CUSTOMER_IDS),
        "auth_keys": {"full": roles.count("full"), "read": roles.count("read")},
        "tools": {"read": sorted(READ_TOOL_NAMES), "write": sorted(WRITE_TOOL_NAMES)},
        "uptime_s": round(time.monotonic() - STARTED_AT),
    }


async def app(scope, receive, send):
    """Enveloppe l'app MCP : /health public, /mcp protege par les cles d'acces."""
    if scope["type"] != "http":
        await _inner_app(scope, receive, send)  # lifespan, etc.
        return

    path = scope.get("path", "").rstrip("/") or "/"

    if path in ("/", "/health"):
        await _send_json(send, 200, health_payload())
        return

    # /mcp/<secret> (connecteur claude.ai) ou /mcp + Authorization: Bearer <secret>
    if path.startswith("/mcp/"):
        user = find_key(path[len("/mcp/"):])
    elif path == "/mcp":
        user = _bearer_user(scope)
    else:
        user = None
    if user is None:
        await _send_json(send, 401, {"error": "unauthorized"})
        return

    scope = dict(scope)
    scope["path"] = "/mcp"
    scope["raw_path"] = b"/mcp"
    scope[AUTH_SCOPE_KEY] = user  # lu par RoleAwareFastMCP (outils selon le role)
    await _inner_app(scope, receive, send)


# ---------------------------------------------------------------------------
# Point d'entree
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for warning in AUTH_KEY_WARNINGS:
        log(f"ATTENTION AUTH_KEYS : {warning}")
    if not AUTH_KEYS:
        raise SystemExit(
            "ERREUR : definir AUTH_KEYS = \"secret:nom:role\" (role full ou read, "
            "secret de 16 caracteres minimum ; plusieurs cles separees par des "
            "virgules). Generer un secret par exemple avec :\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if CUSTOMER_ID_ERRORS:
        raise SystemExit(
            "ERREUR : GOOGLE_ADS_CUSTOMER_ID invalide : " + " ; ".join(CUSTOMER_ID_ERRORS)
        )
    port = int(os.environ.get("PORT", 8080))
    keys = ", ".join(f"{u['name']} ({u['role']})" for u in AUTH_KEYS.values())
    log(f"{SERVICE_NAME} {VERSION}{f' - {BRAND}' if BRAND else ''} : demarrage sur 0.0.0.0:{port}")
    log(f"  - endpoint MCP : /mcp/<secret>  (ou /mcp + Bearer) ; cles : {keys}")
    log(f"  - ecritures autorisees : {ALLOW_WRITES}")
    log(
        "  - compte par defaut : "
        + (SERVED_CUSTOMER_IDS[0] if SERVED_CUSTOMER_IDS else "aucun (customer_id requis)")
        + (f" ; autres comptes servis : {', '.join(SERVED_CUSTOMER_IDS[1:])}"
           if len(SERVED_CUSTOMER_IDS) > 1 else "")
    )
    missing = [v for v in REQUIRED_GOOGLE_VARS if not os.environ.get(v)]
    if missing:
        log(
            "  - ATTENTION : configuration Google Ads incomplete, variables "
            f"manquantes : {', '.join(missing)} (les outils renverront une erreur)"
        )
    uvicorn.run(app, host="0.0.0.0", port=port)
