"""
Test hors ligne des nouveaux outils de main.py (aucun appel a l'API Google Ads).

Ne fait PAS partie du serveur. Lancer depuis la racine du projet :

    python tests/offline_build_test.py

Le test :
  1. construit un GoogleAdsClient "factice" (identifiants bidon, aucun echange
     reseau : le client n'ouvre une connexion qu'au moment d'envoyer une requete) ;
  2. exerce chaque fonction de construction d'operations (campagne Search
     complete, groupe d'annonces, assets, themes de recherche, ROAS cible,
     objectifs de conversion, expansion d'URL, Customer Match : liste, job,
     lots d'operations) sur l'API v22 ET sur la version par defaut de la
     librairie, et verifie noms de champs, enums, nombre et ordre des
     operations ;
  3. verifie la normalisation / le hachage Customer Match sur des vecteurs
     connus (email Gmail, telephone E.164, adresse complete, membre vide) ;
  4. appelle chaque nouvel outil MCP de bout en bout avec run_query et les
     methodes mutate* / OfflineUserDataJobService remplacees par des doublures
     qui enregistrent la requete ;
  5. verifie les cles d'acces (AUTH_KEYS), le compte par defaut et la limite
     aux comptes servis (GOOGLE_ADS_CUSTOMER_ID), y compris dans list_accounts.

La couche HTTP (roles full / read, /health) est testee par tests/http_auth_test.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from types import SimpleNamespace

os.environ["GOOGLE_ADS_ALLOW_WRITES"] = "true"  # active les outils d'ecriture
os.environ["AUTH_KEYS"] = "offline-test-secret-0123456789:tests:full"
# Compte par defaut des outils (customer_id omis) : normalise en CID ci-dessous.
os.environ["GOOGLE_ADS_CUSTOMER_ID"] = "123-456-7890"
os.environ.pop("GOOGLE_ADS_LOGIN_CUSTOMER_ID", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402  (import apres configuration de l'environnement)

from google.ads.googleads.client import GoogleAdsClient, _DEFAULT_VERSION  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402

CID = "1234567890"
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def make_client(version: str | None = None) -> GoogleAdsClient:
    """Client hors ligne : GoogleAdsClient.load_from_dict rafraichit le jeton
    OAuth immediatement (reseau), on passe donc des Credentials deja "valides"."""
    creds = Credentials(
        token="offline-dummy-token",
        refresh_token="x",
        client_id="x",
        client_secret="x",
        token_uri="https://accounts.google.com/o/oauth2/token",
    )
    return GoogleAdsClient(
        credentials=creds, developer_token="x", use_proto_plus=True, version=version
    )


SAMPLE_SPEC = {
    "name": "Test Search - Chalets Quebec",
    "daily_budget": 25.5,
    "status": "PAUSED",
    "bidding": {"type": "MAXIMIZE_CONVERSIONS", "target_cpa": 12.0},
    "geo_target_constant_ids": [20123],
    "language_constant_ids": [1002],
    "negative_keywords": [
        {"text": "gratuit", "match_type": "BROAD"},
        {"text": "emploi", "match_type": "PHRASE"},
        {"text": "formation", "match_type": "EXACT"},
    ],
    "ad_groups": [
        {
            "name": "Chalets",
            "final_url": "https://example.com/chalets",
            "keywords": [
                {"text": "chalet a louer", "match_type": "PHRASE"},
                {"text": "location chalet quebec", "match_type": "EXACT"},
                {"text": "chalet laurentides", "match_type": "BROAD"},
            ],
            "headlines": [
                "Chalets a louer au Quebec",
                "Reservez votre chalet",
                "Nature et confort",
            ],
            "descriptions": [
                "Des chalets tout equipes pour vos vacances en famille ou entre amis.",
                "Reservation simple et rapide, annulation flexible.",
            ],
            "path1": "chalets",
            "path2": "quebec",
        },
        {
            "name": "Spas",
            "final_url": "https://example.com/spas",
            "keywords": [
                {"text": "chalet avec spa", "match_type": "PHRASE"},
                {"text": "spa prive chalet", "match_type": "EXACT"},
            ],
            "headlines": [
                "Chalet avec spa prive",
                "Detente au bord du lac",
                "Spa chauffe toute l'annee",
                "Reservez en ligne",
            ],
            "descriptions": [
                "Un spa prive dans chaque chalet, face a la nature.",
                "Escapade romantique ou sejour entre amis.",
                "Disponibilites en temps reel.",
            ],
        },
    ],
    "sitelinks": [
        {
            "text": "Nos chalets",
            "final_url": "https://example.com/chalets",
            "description1": "Tous nos chalets",
            "description2": "Photos et disponibilites",
        },
        {"text": "Tarifs", "final_url": "https://example.com/tarifs"},
        {
            "text": "Contact",
            "final_url": "https://example.com/contact",
            "description1": "Une question ?",
            "description2": "Ecrivez-nous",
        },
    ],
    "callouts": ["Annulation flexible", "Spa inclus"],
}

EXPECTED_COUNTS = {
    "campaign_budget": 1,
    "campaign": 1,
    "campaign_criterion": 5,  # 1 zone + 1 langue + 3 negatifs
    "ad_group": 2,
    "ad_group_ad": 2,
    "ad_group_criterion": 5,  # 3 + 2 mots-cles
    "asset": 5,  # 3 sitelinks + 2 callouts
    "campaign_asset": 5,
}


# ---------------------------------------------------------------------------
# 1) Fonctions de construction, par version d'API
# ---------------------------------------------------------------------------


def test_search_campaign_ops(client) -> None:
    spec = main.validate_search_campaign_spec(SAMPLE_SPEC)
    ops = main._build_search_campaign_ops(client, CID, spec)
    counts = main.count_ops(ops)
    check(counts == EXPECTED_COUNTS, f"comptes inattendus : {counts}")
    check(len(ops) == sum(EXPECTED_COUNTS.values()) == 26, f"{len(ops)} operations")

    # IDs temporaires : negatifs, uniques, definis avant d'etre references.
    kinds = [op._pb.WhichOneof("operation") for op in ops]
    defined: set[str] = set()
    for op, kind in zip(ops, kinds):
        entity = getattr(op, kind).create
        rn = entity.resource_name
        if rn:
            check(int(rn.rsplit("/", 1)[1]) < 0, f"id temporaire attendu : {rn}")
            check(rn not in defined, f"id temporaire duplique : {rn}")
            defined.add(rn)
        for ref_field in ("campaign_budget", "campaign", "ad_group", "asset"):
            ref = getattr(entity, ref_field, None)  # champs de reference (str)
            if isinstance(ref, str) and "/-" in ref:
                check(ref in defined, f"{kind} reference {ref} avant sa creation")

    budget = ops[0].campaign_budget_operation.create
    check(budget.amount_micros == 25_500_000, "budget en micros")
    check(budget.delivery_method.name == "STANDARD", "delivery_method")
    check(budget.explicitly_shared is False, "budget non partage")
    check(budget.name.startswith("Test Search - Chalets Quebec - budget"), budget.name)

    campaign = ops[1].campaign_operation.create
    check(campaign.name == SAMPLE_SPEC["name"], "nom de campagne")
    check(campaign.status.name == "PAUSED", "statut PAUSED par defaut")
    check(campaign.advertising_channel_type.name == "SEARCH", "canal SEARCH")
    check(campaign.campaign_budget == budget.resource_name, "lien budget")
    check(campaign.maximize_conversions.target_cpa_micros == 12_000_000, "CPA cible en micros")
    check(campaign._pb.WhichOneof("campaign_bidding_strategy") == "maximize_conversions", "strategie")
    ns = campaign.network_settings
    check(
        (ns.target_google_search, ns.target_search_network, ns.target_content_network,
         ns.target_partner_search_network) == (True, False, False, False),
        "network_settings",
    )
    check(campaign.geo_target_type_setting.positive_geo_target_type.name == "PRESENCE", "geo PRESENCE")
    check(campaign.geo_target_type_setting.negative_geo_target_type.name == "PRESENCE", "geo neg PRESENCE")
    check(
        campaign.contains_eu_political_advertising.name
        == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "declaration publicite politique UE",
    )

    criteria = [op.campaign_criterion_operation.create for op in ops if op._pb.WhichOneof("operation") == "campaign_criterion_operation"]
    check(criteria[0].location.geo_target_constant == "geoTargetConstants/20123", "zone geo")
    check(criteria[1].language.language_constant == "languageConstants/1002", "langue")
    negs = criteria[2:]
    check(all(c.negative for c in negs) and [c.keyword.match_type.name for c in negs] == ["BROAD", "PHRASE", "EXACT"], "negatifs")
    check(all(c.campaign == campaign.resource_name for c in criteria), "criteres lies a la campagne")

    ad_groups = [op.ad_group_operation.create for op in ops if op._pb.WhichOneof("operation") == "ad_group_operation"]
    check([g.name for g in ad_groups] == ["Chalets", "Spas"], "noms des groupes")
    check(all(g.type_.name == "SEARCH_STANDARD" and g.status.name == "ENABLED" for g in ad_groups), "type/statut groupe")
    check(all(g.campaign == campaign.resource_name for g in ad_groups), "groupes lies a la campagne")

    ads = [op.ad_group_ad_operation.create for op in ops if op._pb.WhichOneof("operation") == "ad_group_ad_operation"]
    rsa1 = ads[0].ad.responsive_search_ad
    check(len(rsa1.headlines) == 3 and len(rsa1.descriptions) == 2, "RSA 1 : 3 titres, 2 descriptions")
    check(rsa1.headlines[0].text == "Chalets a louer au Quebec", "texte titre")
    check(rsa1.path1 == "chalets" and rsa1.path2 == "quebec", "chemins d'affichage")
    check(list(ads[0].ad.final_urls) == ["https://example.com/chalets"], "URL finale RSA")
    check(ads[0].ad_group == ad_groups[0].resource_name, "RSA liee au groupe 1")
    rsa2 = ads[1].ad.responsive_search_ad
    check(len(rsa2.headlines) == 4 and len(rsa2.descriptions) == 3 and not rsa2.path1, "RSA 2")
    check(ads[1].ad_group == ad_groups[1].resource_name, "RSA liee au groupe 2")

    kws = [op.ad_group_criterion_operation.create for op in ops if op._pb.WhichOneof("operation") == "ad_group_criterion_operation"]
    check([k.keyword.match_type.name for k in kws] == ["PHRASE", "EXACT", "BROAD", "PHRASE", "EXACT"], "match types")
    check([k.ad_group for k in kws] == [ad_groups[0].resource_name] * 3 + [ad_groups[1].resource_name] * 2, "mots-cles lies")
    check(all(k.status.name == "ENABLED" and not k.negative for k in kws), "mots-cles positifs actifs")

    assets = [op.asset_operation.create for op in ops if op._pb.WhichOneof("operation") == "asset_operation"]
    links = [op.campaign_asset_operation.create for op in ops if op._pb.WhichOneof("operation") == "campaign_asset_operation"]
    check(assets[0].sitelink_asset.link_text == "Nos chalets", "sitelink texte")
    check(assets[0].sitelink_asset.description1 == "Tous nos chalets", "sitelink description1")
    check(assets[0].sitelink_asset.description2 == "Photos et disponibilites", "sitelink description2")
    check(list(assets[0].final_urls) == ["https://example.com/chalets"], "sitelink URL")
    check(assets[1].sitelink_asset.link_text == "Tarifs" and not assets[1].sitelink_asset.description1, "sitelink sans descriptions")
    check(assets[3].callout_asset.callout_text == "Annulation flexible", "callout texte")
    check([l.field_type.name for l in links] == ["SITELINK"] * 3 + ["CALLOUT"] * 2, "field_type des liaisons")
    check([l.asset for l in links] == [a.resource_name for a in assets], "liaisons -> assets")
    check(all(l.campaign == campaign.resource_name for l in links), "liaisons -> campagne")

    # La requete complete se construit et se serialise (proto valide).
    request = main.build_mutate_request(client, CID, ops, dry_run=True)
    check(request.validate_only is True and request.customer_id == CID, "validate_only / customer_id")
    check(len(request.mutate_operations) == 26, "26 operations dans la requete")
    check(len(request._pb.SerializeToString()) > 1000, "serialisation")
    check(main.build_mutate_request(client, CID, ops, dry_run=False).validate_only is False, "validate_only False")

    # Variantes d'encheres.
    spec_value = dict(SAMPLE_SPEC, bidding={"type": "MAXIMIZE_CONVERSION_VALUE", "target_roas": 3.0})
    camp = main._build_search_campaign_ops(client, CID, main.validate_search_campaign_spec(spec_value))[1].campaign_operation.create
    check(camp._pb.WhichOneof("campaign_bidding_strategy") == "maximize_conversion_value", "strategie valeur")
    check(camp.maximize_conversion_value.target_roas == 3.0, "ROAS cible")
    spec_tcpa = dict(SAMPLE_SPEC, bidding={"type": "TARGET_CPA", "target_cpa": 8.25})
    camp = main._build_search_campaign_ops(client, CID, main.validate_search_campaign_spec(spec_tcpa))[1].campaign_operation.create
    check(camp.maximize_conversions.target_cpa_micros == 8_250_000, "TARGET_CPA -> max conversions + CPA cible")
    spec_plain = dict(SAMPLE_SPEC, bidding={"type": "MAXIMIZE_CONVERSION_VALUE"}, status="ENABLED")
    camp = main._build_search_campaign_ops(client, CID, main.validate_search_campaign_spec(spec_plain))[1].campaign_operation.create
    check(camp._pb.WhichOneof("campaign_bidding_strategy") == "maximize_conversion_value", "valeur sans cible")
    check(camp.maximize_conversion_value.target_roas == 0.0 and camp.status.name == "ENABLED", "sans cible, ENABLED")


PMAX_SPEC = {
    "name": "PM-CA-EN-Test",
    "daily_budget": 40,
    "status": "ENABLED",
    "geo_target_constant_ids": [2124],
    "geo_target_type": "PRESENCE",
    "language_constant_ids": [1000],
    "negative_keywords": [
        {"text": "outdoor", "match_type": "BROAD"},
        {"text": "post light", "match_type": "PHRASE"},
    ],
    "final_url": "https://example.com/en/pages/studio",
    "asset_group_name": "Studio EN",
    "path1": "studio",
    "headlines": ["Made in Canada", "Hand-cast aluminum", "Designer pendant lights"],
    "long_headlines": ["Cast aluminum pendant lights, hand-poured in our Canadian foundry"],
    "descriptions": [
        "Cast aluminum pendant lights, made in Canada.",
        "Sculptural centrepieces that pair warm minimalism with lasting durability. Made in Canada.",
    ],
    "business_name": "Marque test",
    "logo_asset_ids": [5001],
    "landscape_logo_asset_ids": [5002],
    "marketing_image_asset_ids": [6001, 6002],
    "square_marketing_image_asset_ids": [6003],
    "portrait_marketing_image_asset_ids": [6004],
    "youtube_video_asset_ids": [7001],
    "merchant_id": 123456789,
    "feed_label": "CA",
    "enable_local": True,
    "listing_group": {"include_item_ids": ["shopify_ca_1_10", "shopify_ca_1_11", "shopify_ca_1_10"]},
    "search_themes": ["pendant light", "made in canada lighting"],
    "audience_ids": [5550001],
    "sitelinks": [{"text": "ALFA Pendant", "final_url": "https://example.com/en/products/alfa"}],
    "callouts": ["Made in Canada", "Six Colours"],
}

PMAX_EXPECTED_COUNTS = {
    "campaign_budget": 1,
    "campaign": 1,
    "asset": 1 + 3 + 1 + 2 + 1 + 2,  # nom d'entreprise, textes, sitelink, callouts
    "campaign_asset": 1 + 1 + 1 + 1 + 2,  # nom, logo, logo paysage, sitelink, callouts
    "campaign_criterion": 4,  # zone, langue, 2 negatifs
    "asset_group": 1,
    "asset_group_asset": 6 + 2 + 1 + 1 + 1,  # textes + images/videos existants
    "asset_group_listing_group_filter": 1 + 2 + 1,  # racine, 2 fiches, "autres"
    "asset_group_signal": 3,  # 2 themes + 1 audience
}


def test_pmax_campaign_ops(client) -> None:
    spec = main.finalize_pmax_spec(main.validate_pmax_campaign_spec(PMAX_SPEC), None)
    check(spec["include_item_ids"] == ["shopify_ca_1_10", "shopify_ca_1_11"], "doublon d'item id retire")
    check(spec["assets"]["MARKETING_IMAGE"] == ["6001", "6002"] and spec["audience_ids"] == ["5550001"], "ids en chaines")
    check(spec["cloned"] == {} and spec["merchant_id"] == "123456789", "sans clonage")
    ops = main._build_pmax_campaign_ops(client, CID, spec)
    counts = main.count_ops(ops)
    check(counts == PMAX_EXPECTED_COUNTS, f"comptes PMax inattendus : {counts}")

    kinds = [op._pb.WhichOneof("operation") for op in ops]
    defined: set[str] = set()
    for op, kind in zip(ops, kinds):
        entity = getattr(op, kind).create
        rn = entity.resource_name
        if rn:
            check(rn not in defined, f"id temporaire duplique : {rn}")
            defined.add(rn)
        for ref_field in ("campaign_budget", "campaign", "asset_group", "asset", "parent_listing_group_filter"):
            ref = getattr(entity, ref_field, None)
            if isinstance(ref, str) and "/-" in ref:
                check(ref in defined, f"{kind} reference {ref} avant sa creation")

    budget = ops[0].campaign_budget_operation.create
    check(budget.amount_micros == 40_000_000, "budget PMax en micros")
    campaign = ops[1].campaign_operation.create
    check(campaign.advertising_channel_type.name == "PERFORMANCE_MAX", "canal PERFORMANCE_MAX")
    check(campaign.status.name == "ENABLED" and campaign.name == "PM-CA-EN-Test", "statut / nom")
    check(campaign._pb.WhichOneof("campaign_bidding_strategy") == "maximize_conversion_value", "strategie valeur")
    check(campaign.maximize_conversion_value.target_roas == 0.0, "sans ROAS cible")
    check(campaign.shopping_setting.merchant_id == 123456789 and campaign.shopping_setting.feed_label == "CA", "reglages marchand")
    check(campaign.shopping_setting.enable_local is True, "enable_local repris")
    spec_nolocal = main.finalize_pmax_spec(main.validate_pmax_campaign_spec(dict(PMAX_SPEC, enable_local=False)), None)
    camp_nolocal = main._build_pmax_campaign_ops(client, CID, spec_nolocal)[1].campaign_operation.create
    check(not camp_nolocal._pb.shopping_setting.HasField("enable_local"), "enable_local False : champ non envoye")
    check(campaign.brand_guidelines_enabled is True, "brand guidelines")
    check(campaign.geo_target_type_setting.positive_geo_target_type.name == "PRESENCE", "geo PRESENCE")
    settings = campaign.asset_automation_settings
    check(len(settings) == 1 and settings[0].asset_automation_type.name == main.FINAL_URL_EXPANSION_SETTING and settings[0].asset_automation_status.name == "OPTED_OUT", "expansion d'URL desactivee")
    check(campaign.contains_eu_political_advertising.name == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING", "declaration UE")

    campaign_assets = [op.campaign_asset_operation.create for op in ops if op._pb.WhichOneof("operation") == "campaign_asset_operation"]
    check([a.field_type.name for a in campaign_assets] == ["BUSINESS_NAME", "LOGO", "LANDSCAPE_LOGO", "SITELINK", "CALLOUT", "CALLOUT"], "assets de campagne")
    check(campaign_assets[1].asset == f"customers/{CID}/assets/5001" and campaign_assets[2].asset == f"customers/{CID}/assets/5002", "logos existants relies")
    business_asset = ops[2].asset_operation.create
    check(business_asset.text_asset.text == "Marque test" and campaign_assets[0].asset == business_asset.resource_name, "nom d'entreprise cree puis relie")
    check(all(a.campaign == campaign.resource_name for a in campaign_assets), "assets -> campagne")

    criteria = [op.campaign_criterion_operation.create for op in ops if op._pb.WhichOneof("operation") == "campaign_criterion_operation"]
    check(criteria[0].location.geo_target_constant == "geoTargetConstants/2124", "zone Canada")
    check(criteria[1].language.language_constant == "languageConstants/1000", "langue anglais")
    check(criteria[2].negative and criteria[3].keyword.match_type.name == "PHRASE", "negatifs")

    groups = [op.asset_group_operation.create for op in ops if op._pb.WhichOneof("operation") == "asset_group_operation"]
    group = groups[0]
    check(group.name == "Studio EN" and group.campaign == campaign.resource_name and group.status.name == "PAUSED", "groupe d'assets cree en PAUSE")
    status_op = main._build_asset_group_status_op(client, f"customers/{CID}/assetGroups/555", "ENABLED")
    check(status_op.update.resource_name == f"customers/{CID}/assetGroups/555" and status_op.update.status.name == "ENABLED", "activation du groupe")
    check(list(status_op.update_mask.paths) == ["status"], "masque statut")
    check(list(group.final_urls) == ["https://example.com/en/pages/studio"] and group.path1 == "studio" and not group.path2, "URL finale / chemin")
    check(group.resource_name == f"customers/{CID}/assetGroups/-10", f"id temporaire du groupe : {group.resource_name}")
    # Les assets texte precedent le groupe ; les liaisons forment un bloc contigu apres le groupe.
    kinds_seq = [op._pb.WhichOneof("operation") for op in ops]
    g_index = kinds_seq.index("asset_group_operation")
    check(all(k != "asset_group_asset_operation" for k in kinds_seq[:g_index]), "aucune liaison avant le groupe")
    n_links = kinds_seq.count("asset_group_asset_operation")
    check(kinds_seq[g_index + 1:g_index + 1 + n_links] == ["asset_group_asset_operation"] * n_links, "liaisons contigues apres le groupe")
    check(all(k != "asset_operation" for k in kinds_seq[g_index + 1:g_index + 1 + n_links]), "pas d'asset texte entre les liaisons")

    links = [op.asset_group_asset_operation.create for op in ops if op._pb.WhichOneof("operation") == "asset_group_asset_operation"]
    check([l.field_type.name for l in links] == ["HEADLINE"] * 3 + ["LONG_HEADLINE"] + ["DESCRIPTION"] * 2 + ["MARKETING_IMAGE"] * 2 + ["SQUARE_MARKETING_IMAGE", "PORTRAIT_MARKETING_IMAGE", "YOUTUBE_VIDEO"], f"types des liaisons : {[l.field_type.name for l in links]}")
    check(all(l.asset_group == group.resource_name for l in links), "liaisons -> groupe")
    texts = [op.asset_operation.create for op in ops if op._pb.WhichOneof("operation") == "asset_operation"]
    check(texts[1].text_asset.text == "Made in Canada" and links[0].asset == texts[1].resource_name, "titre cree puis relie")
    check(ops.index(next(op for op in ops if op._pb.WhichOneof("operation") == "asset_operation" and op.asset_operation.create.text_asset.text == "Made in Canada")) < g_index, "asset texte avant le groupe")
    check(links[6].asset == f"customers/{CID}/assets/6001" and links[-1].asset == f"customers/{CID}/assets/7001", "images/videos existantes")

    filters = [op.asset_group_listing_group_filter_operation.create for op in ops if op._pb.WhichOneof("operation") == "asset_group_listing_group_filter_operation"]
    root, leaf1, leaf2, other = filters
    check(root.type_.name == "SUBDIVISION" and not root.parent_listing_group_filter, "racine subdivision")
    check(root.resource_name.startswith(f"customers/{CID}/assetGroupListingGroupFilters/-10~-"), f"resource name racine : {root.resource_name}")
    check(all(f.asset_group == group.resource_name and f.listing_source.name == "SHOPPING" for f in filters), "filtres -> groupe, source SHOPPING")
    check(leaf1.parent_listing_group_filter == root.resource_name and leaf1.type_.name == "UNIT_INCLUDED", "feuille incluse")
    check(leaf1.case_value.product_item_id.value == "shopify_ca_1_10" and leaf2.case_value.product_item_id.value == "shopify_ca_1_11", "item ids")
    check(other.type_.name == "UNIT_EXCLUDED" and other.parent_listing_group_filter == root.resource_name, "noeud 'autres' exclu")
    check(other._pb.case_value.HasField("product_item_id") and not other.case_value.product_item_id.value, "'autres' : product_item_id present mais vide")
    check(len({f.resource_name for f in filters}) == 4, "ids temporaires de filtres distincts")

    signals = [op.asset_group_signal_operation.create for op in ops if op._pb.WhichOneof("operation") == "asset_group_signal_operation"]
    check([s._pb.WhichOneof("signal") for s in signals] == ["search_theme", "search_theme", "audience"], "signaux")
    check(signals[0].search_theme.text == "pendant light" and signals[2].audience.audience == f"customers/{CID}/audiences/5550001", "contenu des signaux")
    check(all(s.asset_group == group.resource_name for s in signals), "signaux -> groupe")

    request = main.build_mutate_request(client, CID, ops, dry_run=True)
    check(request.validate_only and len(request.mutate_operations) == sum(PMAX_EXPECTED_COUNTS.values()), "requete PMax")
    check(len(request._pb.SerializeToString()) > 1500, "serialisation PMax")

    # Variante : tout le flux, ROAS cible, nom d'entreprise existant, sans expansion opt-out.
    variant = dict(PMAX_SPEC, target_roas=4.5, business_name=None, business_name_asset_id="8001",
                   url_expansion_opt_out=False, status="PAUSED", geo_target_type="PRESENCE_OR_INTEREST")
    variant.pop("listing_group")
    spec2 = main.finalize_pmax_spec(main.validate_pmax_campaign_spec(variant), None)
    ops2 = main._build_pmax_campaign_ops(client, CID, spec2)
    counts2 = main.count_ops(ops2)
    check(counts2["asset_group_listing_group_filter"] == 1 and counts2["asset"] == PMAX_EXPECTED_COUNTS["asset"] - 1, f"variante : {counts2}")
    camp2 = ops2[1].campaign_operation.create
    check(camp2.maximize_conversion_value.target_roas == 4.5 and camp2.status.name == "PAUSED", "ROAS cible / PAUSED")
    check(len(camp2.asset_automation_settings) == 0, "pas de reglage d'automatisation")
    check(camp2.geo_target_type_setting.positive_geo_target_type.name == "PRESENCE_OR_INTEREST", "geo interet")
    root2 = [op.asset_group_listing_group_filter_operation.create for op in ops2 if op._pb.WhichOneof("operation") == "asset_group_listing_group_filter_operation"][0]
    check(root2.type_.name == "UNIT_INCLUDED" and not root2._pb.case_value.HasField("product_item_id"), "tout le flux : une feuille sans case_value")
    first_campaign_asset = [op.campaign_asset_operation.create for op in ops2 if op._pb.WhichOneof("operation") == "campaign_asset_operation"][0]
    check(first_campaign_asset.asset == f"customers/{CID}/assets/8001", "nom d'entreprise existant relie")


def test_pmax_validation_errors() -> None:
    def expect_error(spec, fragment: str) -> None:
        try:
            main.finalize_pmax_spec(main.validate_pmax_campaign_spec(spec), None)
        except main.SpecError as ex:
            check(fragment in str(ex), f"message inattendu pour {fragment!r} : {ex}")
        else:
            raise AssertionError(f"SpecError attendue ({fragment!r})")

    expect_error(dict(PMAX_SPEC, final_url="exemple.com"), "http://")
    expect_error(dict(PMAX_SPEC, headlines=["a", "b"]), "attendu entre 3 et 15")
    expect_error(dict(PMAX_SPEC, headlines=["x" * 31, "b", "c"]), "maximum 30")
    expect_error(dict(PMAX_SPEC, long_headlines=[]), "attendu entre 1 et 5")
    expect_error(dict(PMAX_SPEC, descriptions=["d" * 70, "e" * 70]), "60 caracteres ou moins")
    expect_error(dict(PMAX_SPEC, target_roas=400), "ratio attendu")
    expect_error(dict(PMAX_SPEC, geo_target_constant_ids=[]), "geo_target_constant_ids")
    expect_error(dict(PMAX_SPEC, geo_target_type="RADIUS"), "PRESENCE")
    expect_error(dict(PMAX_SPEC, listing_group={"include_item_ids": []}), "include_item_ids")
    expect_error(dict(PMAX_SPEC, business_name="x" * 26), "maximum 25")
    expect_error(dict(PMAX_SPEC, business_name="A", business_name_asset_id=5), "exclusifs")
    expect_error(dict(PMAX_SPEC, search_themes=["t"] * 26), "doublon")
    expect_error(dict(PMAX_SPEC, youtube_video_asset_ids=[1, 2, 3, 4, 5, 6]), "maximum 5")
    expect_error(dict(PMAX_SPEC, url_expansion_opt_out="oui"), "booleen")
    expect_error(dict(PMAX_SPEC, marketing_image_asset_ids=[]), "marketing_image_asset_ids")
    expect_error(dict(PMAX_SPEC, logo_asset_ids=[], business_name=None), "logo_asset_ids")
    no_merchant = dict(PMAX_SPEC)
    no_merchant.pop("merchant_id")
    expect_error(no_merchant, "merchant_id")
    check(main.validate_pmax_campaign_spec(dict(PMAX_SPEC, clone_from_campaign_id="77"))["clone_from_campaign_id"] == "77", "clone id normalise")


def test_ad_group_and_asset_ops(client) -> None:
    import itertools

    campaign_rn = f"customers/{CID}/campaigns/42"
    ad_group = main.validate_ad_group_spec(SAMPLE_SPEC["ad_groups"][0])
    ops = main._build_ad_group_ops(client, CID, campaign_rn, ad_group, itertools.count(-1, -1))
    check(main.count_ops(ops) == {"ad_group": 1, "ad_group_ad": 1, "ad_group_criterion": 3}, "ops groupe seul")
    check(ops[0].ad_group_operation.create.campaign == campaign_rn, "groupe -> campagne existante")
    check(ops[0].ad_group_operation.create.resource_name == f"customers/{CID}/adGroups/-1", "id temporaire -1")

    links = main.validate_sitelinks(SAMPLE_SPEC["sitelinks"])
    texts = main.validate_callouts(SAMPLE_SPEC["callouts"])
    ops = main._build_campaign_asset_ops(client, CID, campaign_rn, links, texts, itertools.count(-1, -1))
    check(main.count_ops(ops) == {"asset": 5, "campaign_asset": 5}, "ops assets seuls")
    check(ops[0].asset_operation.create.resource_name == f"customers/{CID}/assets/-1", "asset temp id")
    check(ops[1].campaign_asset_operation.create.asset == f"customers/{CID}/assets/-1", "liaison asset -1")
    check(ops[1].campaign_asset_operation.create.campaign == campaign_rn, "liaison campagne existante")
    check(ops[-1].campaign_asset_operation.create.field_type.name == "CALLOUT", "dernier = callout")


def test_search_theme_ops(client) -> None:
    ops = main._build_search_theme_ops(client, CID, "777", ["chalet a louer", "spa nature", "location laurentides"])
    check(len(ops) == 3, "3 themes")
    check(ops[0].create.asset_group == f"customers/{CID}/assetGroups/777", "asset_group")
    check(ops[0].create.search_theme.text == "chalet a louer", "texte du theme")
    check(ops[0].create._pb.WhichOneof("signal") == "search_theme", "oneof signal")
    request = client.get_type("MutateAssetGroupSignalsRequest")
    request.customer_id = CID
    for op in ops:
        request.operations.append(op)
    request.validate_only = True
    check(len(request.operations) == 3 and request.validate_only, "requete signaux")


def test_target_roas_ops(client) -> None:
    op = main._build_target_roas_op(client, CID, "42", 3.0)
    check(op.update.resource_name == f"customers/{CID}/campaigns/42", "resource name")
    check(op.update.maximize_conversion_value.target_roas == 3.0, "valeur ROAS")
    check("maximize_conversion_value.target_roas" in list(op.update_mask.paths), f"masque : {list(op.update_mask.paths)}")
    op = main._build_target_roas_op(client, CID, "42", None)
    check(list(op.update_mask.paths) == ["maximize_conversion_value.target_roas"], "masque effacement")
    check(op.update.maximize_conversion_value.target_roas == 0.0, "valeur effacee")
    check(op.update._pb.WhichOneof("campaign_bidding_strategy") == "maximize_conversion_value", "oneof conserve")
    request = client.get_type("MutateCampaignsRequest")
    request.operations.append(op)
    request.validate_only = True
    check(len(request.operations) == 1, "requete campagne")


def test_conversion_goal_ops(client) -> None:
    op = main._build_goal_config_op(client, CID, "42")
    check(op.update.resource_name == f"customers/{CID}/conversionGoalCampaignConfigs/42", "config resource name")
    check(op.update.goal_config_level.name == "CAMPAIGN", "niveau CAMPAIGN")
    check("goal_config_level" in list(op.update_mask.paths), "masque config")
    request = client.get_type("MutateConversionGoalCampaignConfigsRequest")
    request.operations.append(op)
    request.validate_only = True

    goals = [
        {"category": "PURCHASE", "origin": "WEBSITE", "biddable": False},
        {"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": True},
        {"category": "PAGE_VIEW", "origin": "WEBSITE", "biddable": False},
        {"category": "PURCHASE", "origin": "APP", "biddable": True},
    ]
    ops, changes = main._build_conversion_goal_ops(client, CID, "42", goals, ["PURCHASE"])
    check(len(ops) == 2 and len(changes) == 2, f"2 changements attendus : {changes}")
    check(ops[0].update.resource_name == f"customers/{CID}/campaignConversionGoals/42~PURCHASE~WEBSITE", "goal resource name")
    check(ops[0].update.biddable is True and ops[1].update.biddable is False, "valeurs biddable")
    check(all(list(op.update_mask.paths) == ["biddable"] for op in ops), "masque biddable explicite")
    check(ops[1].update.resource_name.endswith("42~ADD_TO_CART~WEBSITE"), "ADD_TO_CART devient non biddable")
    request = client.get_type("MutateCampaignConversionGoalsRequest")
    for op in ops:
        request.operations.append(op)
    request.validate_only = False
    check(len(request.operations) == 2, "requete objectifs")
    for name in ("PURCHASE", "BEGIN_CHECKOUT", "ADD_TO_CART", "PAGE_VIEW", "DEFAULT", "SUBMIT_LEAD_FORM"):
        getattr(client.enums.ConversionActionCategoryEnum, name)


# Etat representatif d'un compte (valeurs fictives) : objectifs du compte,
# campagne App d'installations avec ses propres actions (selective_optimization).
APP_ID = "1000000001"
ACCOUNT_GOALS = [
    {"category": c, "origin": o, "biddable": b}
    for c, o, b in [
        ("ADD_TO_CART", "WEBSITE", True), ("BEGIN_CHECKOUT", "WEBSITE", True),
        ("CONTACT", "WEBSITE", True), ("DEFAULT", "WEBSITE", True),
        ("DOWNLOAD", "APP", True), ("SUBSCRIBE_PAID", "APP", True),
        ("ENGAGEMENT", "YOUTUBE_HOSTED", True), ("YOUTUBE_FOLLOW_ON_VIEWS", "YOUTUBE_HOSTED", True),
        ("PHONE_CALL_LEAD", "CALL_FROM_ADS", True), ("PURCHASE", "WEBSITE", True),
        ("PAGE_VIEW", "WEBSITE", False), ("CONTACT", "GOOGLE_HOSTED", False),
    ]
]
# Plan d'utilisation, etape 1 : PURCHASE/WEBSITE seul biddable. La campagne
# App optimise sur ses propres actions : elle n'a besoin d'aucun objectif du compte.
PLAN_GOALS = [
    {"category": g["category"], "origin": g["origin"],
     "biddable": (g["category"], g["origin"]) == ("PURCHASE", "WEBSITE")}
    for g in ACCOUNT_GOALS
]
APP_ACTION_RN = f"customers/{CID}/conversionActions/2000000001"
APP_CAMPAIGN = {
    "id": APP_ID, "name": "App - installations", "status": "ENABLED",
    "bidding_strategy_type": "TARGET_CPA",
    "app_campaign_setting": {"bidding_strategy_goal_type": "OPTIMIZE_INSTALLS_TARGET_INSTALL_COST"},
    "selective_optimization": {"conversion_actions": [APP_ACTION_RN]},
}
APP_ACTIONS = {APP_ACTION_RN: {"name": "Installations Android", "category": "DOWNLOAD", "origin": "APP"}}
INHERITING = [{"id": "1000000003", "name": "Recherche - marque", "status": "ENABLED",
               "channel_type": "SEARCH", "bidding_strategy": "MAXIMIZE_CONVERSION_VALUE"}]


def test_customer_goal_ops(client) -> None:
    changes, after = main.plan_customer_goal_changes(PLAN_GOALS, ACCOUNT_GOALS)
    check(len(changes) == 9, f"9 objectifs passent en secondaire : {changes}")
    check(all(c["biddable_before"] and not c["biddable_after"] for c in changes), "sens des changements")
    check([f"{g['category']}/{g['origin']}" for g in after if g["biddable"]] == ["PURCHASE/WEBSITE"], "etat apres")
    check(len(after) == len(ACCOUNT_GOALS), "objectifs non cites conserves")
    # Entree souple : minuscules, booleens texte ; doublon identique accepte.
    changes2, _ = main.plan_customer_goal_changes(
        [{"category": " add_to_cart ", "origin": " website ", "biddable": "false"},
         {"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": False},
         {"category": "PURCHASE", "origin": "WEBSITE", "biddable": True}],
        ACCOUNT_GOALS,
    )
    check(changes2 == [{"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable_before": True, "biddable_after": False}], f"entree souple : {changes2}")

    def expect_error(goals, fragment: str) -> None:
        try:
            main.plan_customer_goal_changes(goals, ACCOUNT_GOALS)
        except main.SpecError as ex:
            check(fragment in str(ex), f"message attendu {fragment!r} : {ex}")
            return
        raise AssertionError(f"erreur attendue ({fragment}) pour {goals}")

    expect_error([], "liste non vide")
    expect_error([{"category": "PURCHASE", "origin": "APP", "biddable": True}], "n'existe pas")
    expect_error([{"category": "PURCHASE", "biddable": True}], "obligatoires")
    expect_error([{"category": "PURCHASE", "origin": "WEBSITE"}], "biddable est obligatoire")
    expect_error([{"category": "PURCHASE", "origin": "WEBSITE", "biddable": "peut-etre"}], "booleen")
    expect_error([{"category": "PURCHASE", "origin": "WEBSITE", "biddable": True},
                  {"category": "PURCHASE", "origin": "WEBSITE", "biddable": False}], "deux fois")

    ops = main._build_customer_goal_ops(client, CID, changes)
    check(len(ops) == 9, "une operation par changement")
    op = ops[0]
    check(op.update.resource_name == f"customers/{CID}/customerConversionGoals/{changes[0]['category']}~{changes[0]['origin']}", f"resource name : {op.update.resource_name}")
    check(op.update.biddable is False and list(op.update_mask.paths) == ["biddable"], "biddable False + masque explicite")
    request = client.get_type("MutateCustomerConversionGoalsRequest")
    request.customer_id = CID
    request.operations.extend(ops)
    request.validate_only = True
    check(len(request.operations) == 9, "requete objectifs du compte")

    # Retour arriere : secondaire -> biddable ("true" en texte compris).
    back, back_after = main.plan_customer_goal_changes(
        [{"category": "PAGE_VIEW", "origin": "WEBSITE", "biddable": "true"},
         {"category": "CONTACT", "origin": "GOOGLE_HOSTED", "biddable": True},
         {"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": False}],
        ACCOUNT_GOALS,
    )
    check([(c["category"], c["biddable_after"]) for c in back] == [("PAGE_VIEW", True), ("CONTACT", True), ("ADD_TO_CART", False)], f"retour arriere : {back}")
    back_ops = main._build_customer_goal_ops(client, CID, back)
    check([o.update.biddable for o in back_ops] == [True, True, False], "valeurs envoyees True/True/False")
    check(all(list(o.update_mask.paths) == ["biddable"] for o in back_ops), "masque biddable partout")
    check(("PAGE_VIEW", "WEBSITE", True) in {(g["category"], g["origin"], g["biddable"]) for g in back_after}, "etat apres True")

    def analyze(campaign, levels, after_state, actions=APP_ACTIONS, inheriting=INHERITING, before=ACCOUNT_GOALS):
        return main.analyze_app_campaigns([campaign], levels, actions, before, after_state, inheriting)

    def plan(goals, current=ACCOUNT_GOALS):
        return main.plan_customer_goal_changes(goals, current)[1]

    # 1) Campagne App avec ses propres actions : independante, aucune alerte,
    #    meme si DOWNLOAD/APP passe en secondaire au niveau du compte.
    summary, warnings, notices = analyze(APP_CAMPAIGN, {}, after)
    check(warnings == [] and summary[0]["depends_on_account_goals"] is False, f"App independante : {summary} {warnings}")
    check(summary[0]["selective_optimization_actions"][0]["category"] == "DOWNLOAD", "action App resolue")
    check(len(notices) == 1 and "independante" in notices[0] and "Installations Android" in notices[0], f"avis App : {notices}")
    check("ne lui retire rien" in summary[0]["note"] and "SUIT" not in summary[0]["note"], f"note independante : {summary[0]['note']}")
    # 2) DOWNLOAD/APP garde biddable sans campagne App qui en ait besoin :
    #    avis sur les campagnes au niveau CUSTOMER qui continueraient d'encherir dessus.
    keep_after = plan([dict(g, biddable=(g["category"], g["origin"]) in {("PURCHASE", "WEBSITE"), ("DOWNLOAD", "APP")}) for g in ACCOUNT_GOALS])
    _, warnings, notices = analyze(APP_CAMPAIGN, {}, keep_after)
    check(warnings == [] and any("DOWNLOAD/APP" in n and "aucune campagne App" in n and "1000000003" in n for n in notices), f"avis objectif APP inutile : {notices}")
    _, warnings, notices = analyze(APP_CAMPAIGN, {}, keep_after, inheriting=[])
    check(warnings == [] and not any("aucune campagne App" in n for n in notices), f"pas d'avis sans campagne CUSTOMER : {notices}")
    # 3) Campagne App au niveau CUSTOMER (meme avec des actions choisies) :
    #    dependante ; DOWNLOAD/APP seul coupe = alerte, meme si SUBSCRIBE_PAID/APP reste.
    dl_after = plan([{"category": "DOWNLOAD", "origin": "APP", "biddable": False}])
    summary, warnings, notices = analyze(APP_CAMPAIGN, {APP_ID: "CUSTOMER"}, dl_after)
    check(summary[0]["depends_on_account_goals"] is True, "niveau CUSTOMER = dependante")
    check(len(warnings) == 1 and "DOWNLOAD/APP" in warnings[0], f"alerte installations (CUSTOMER) : {warnings}")
    check(any("depend des objectifs du compte" in n for n in notices), "dependance signalee dans notices")
    check("SUIT" in summary[0]["note"] and "CUSTOMER" in summary[0]["note"] and "retire rien" not in summary[0]["note"], f"note CUSTOMER : {summary[0]['note']}")
    # SUBSCRIBE_PAID/APP reste biddable sans qu'une App d'installations en ait besoin : avis par objectif.
    check(any("SUBSCRIBE_PAID/APP" in n and "aucune campagne App" in n for n in notices), f"avis par objectif : {notices}")
    check(not any("DOWNLOAD/APP" in n and "aucune campagne App" in n for n in notices), "pas d'avis sur l'objectif utile")
    no_tic = dict(APP_CAMPAIGN, app_campaign_setting={"bidding_strategy_goal_type": "OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST"})
    _, warnings, _ = analyze(no_tic, {APP_ID: "CUSTOMER"}, dl_after)
    check(len(warnings) == 1 and "DOWNLOAD/APP" in warnings[0], f"alerte installations sans CPI cible : {warnings}")
    # 4) Sans ligne de config et sans action choisie : dependante aussi.
    no_selection = dict(APP_CAMPAIGN, selective_optimization={})
    summary, warnings, _ = analyze(no_selection, {}, dl_after, actions={})
    check(summary[0]["depends_on_account_goals"] is True and len(warnings) == 1, f"sans action choisie : {warnings}")
    check("SUIT" in summary[0]["note"] and "aucune action choisie" in summary[0]["note"], f"note sans action : {summary[0]['note']}")
    # Dependante mais DOWNLOAD/APP garde : aucune alerte, pas d'avis "inutile".
    summary, warnings, notices = analyze(no_selection, {}, keep_after, actions={})
    check(warnings == [] and summary[0]["depends_on_account_goals"] is True, "dependante, objectif garde")
    check(not any("aucune campagne App" in n for n in notices), f"objectif APP utile : pas d'avis 'inutile' : {notices}")
    # Installations garde + SUBSCRIBE_PAID/APP biddable : avis sur SUBSCRIBE_PAID seulement.
    _, warnings, notices = analyze(no_selection, {}, plan([{"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": False}]), actions={})
    unneeded = [n for n in notices if "aucune campagne App" in n]
    check(warnings == [] and len(unneeded) == 1 and "SUBSCRIBE_PAID/APP" in unneeded[0] and "DOWNLOAD/APP" not in unneeded[0], f"avis par objectif (dependante) : {notices}")
    # Objectif deja secondaire avant le changement : pas une perte, pas de blocage.
    dl_off = [dict(g, biddable=False) if (g["category"], g["origin"]) == ("DOWNLOAD", "APP") else g for g in ACCOUNT_GOALS]
    unrelated_after = plan([{"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": False}], dl_off)
    _, warnings, notices = analyze(no_selection, {}, unrelated_after, actions={}, before=dl_off)
    check(warnings == [] and any("deja pas biddable" in n for n in notices), f"objectif deja secondaire : {warnings} {notices}")
    # 5) Campagne App d'actions dans l'application : un objectif APP autre que DOWNLOAD.
    in_app = dict(no_selection, app_campaign_setting={"bidding_strategy_goal_type": "OPTIMIZE_IN_APP_CONVERSIONS_TARGET_CONVERSION_COST"})
    _, warnings, _ = analyze(in_app, {}, dl_after, actions={})
    check(warnings == [], f"in-app : SUBSCRIBE_PAID/APP suffit : {warnings}")
    sub_off_after = plan([{"category": "SUBSCRIBE_PAID", "origin": "APP", "biddable": False}])
    _, warnings, _ = analyze(in_app, {}, sub_off_after, actions={})
    check(len(warnings) == 1 and "autre que DOWNLOAD" in warnings[0], f"in-app : DOWNLOAD/APP seul ne suffit pas : {warnings}")
    # 6) Action choisie introuvable : signalee, campagne toujours independante.
    summary, warnings, _ = analyze(APP_CAMPAIGN, {}, after, actions={})
    check(summary[0]["unresolved_actions"] == [APP_ACTION_RN] and warnings == [], "action non resolue signalee")


def test_brand_ops(client) -> None:
    ops = main._build_brand_list_ops(client, CID, "Marque - LBC", ["b-1", "b-2"])
    check(main.count_ops(ops) == {"shared_set": 1, "shared_criterion": 2}, f"operations : {main.count_ops(ops)}")
    shared_set = ops[0].shared_set_operation.create
    check(shared_set.resource_name == f"customers/{CID}/sharedSets/-1", "ID temporaire")
    check(shared_set.type_.name == "BRANDS" and shared_set.name == "Marque - LBC", "type BRANDS")
    for op, brand in zip(ops[1:], ["b-1", "b-2"]):
        crit = op.shared_criterion_operation.create
        check(crit.shared_set == shared_set.resource_name and crit.brand.entity_id == brand, "BrandInfo")
        check(crit._pb.WhichOneof("criterion") == "brand", "critere de type brand")
    request = main.build_mutate_request(client, CID, ops, dry_run=True)
    check(request.validate_only and len(request.mutate_operations) == 3, "requete atomique validate_only")

    rn = f"customers/{CID}/sharedSets/555"
    op = main._build_brand_exclusion_op(client, CID, "1000000002", rn)
    crit = op.create
    check(crit.campaign == f"customers/{CID}/campaigns/1000000002", "campagne")
    check(crit.negative is True and crit.brand_list.shared_set == rn, "exclusion BrandListInfo")
    check(crit._pb.WhichOneof("criterion") == "brand_list", "critere brand_list")
    request = client.get_type("MutateCampaignCriteriaRequest")
    request.operations.append(op)

    check(main._shared_set_id("555") == "555" and main._shared_set_id(rn) == "555", "ID de liste")
    for bad in ("", "abc", "customers/1/campaigns/2"):
        try:
            main._shared_set_id(bad)
        except main.SpecError:
            continue
        raise AssertionError(f"shared_set invalide accepte : {bad!r}")
    check(main._clean_brand_entity_ids(["a", " a ", "b"]) == ["a", "b"], "IDs de marque dedoublonnes")


def test_url_expansion_ops(client) -> None:
    current = [
        {"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_OUT"},
        {"asset_automation_type": "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_IN"},
        {"asset_automation_type": "UNKNOWN", "asset_automation_status": "OPTED_IN"},
    ]
    settings, previous = main.merge_automation_settings(current, main.FINAL_URL_EXPANSION_SETTING, "OPTED_OUT")
    check(previous == "OPTED_IN", "ancien statut")
    check(settings == [("TEXT_ASSET_AUTOMATION", "OPTED_OUT"), (main.FINAL_URL_EXPANSION_SETTING, "OPTED_OUT")], f"fusion : {settings}")
    settings2, previous2 = main.merge_automation_settings([], main.FINAL_URL_EXPANSION_SETTING, "OPTED_IN")
    check(settings2 == [(main.FINAL_URL_EXPANSION_SETTING, "OPTED_IN")] and previous2 is None, "fusion liste vide")
    op = main._build_asset_automation_op(client, CID, "42", settings)
    check(len(op.update.asset_automation_settings) == 2, "2 reglages")
    check(op.update.asset_automation_settings[1].asset_automation_type.name == main.FINAL_URL_EXPANSION_SETTING, "type")
    check(op.update.asset_automation_settings[1].asset_automation_status.name == "OPTED_OUT", "statut")
    check(list(op.update_mask.paths) == ["asset_automation_settings"], "masque liste complete")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


MEMBERS = [
    {"email": " Foo.Bar@Gmail.com "},  # email seul -> 1 identifiant
    {  # tout : email + telephone + adresse complete -> 3 identifiants
        "email": "Jean.Tremblay@Example.COM ", "phone": "(514) 555-0199",
        "first_name": " Jean ", "last_name": "TREMBLAY", "country_code": "ca", "postal_code": "h2x 1y4",
    },
    {"first_name": "Marie", "last_name": "Roy", "country_code": "CA"},  # adresse incomplete -> ignore
    {},  # rien -> ignore
    {"Email": "Foo.Bar@gmail.com"},  # en-tete du gabarit Google ; doublon exact du 1er
]


def make_partial_failure_status(client, indexes: list[int]):
    """google.rpc.Status tel que renvoye dans partial_failure_error, avec une
    GoogleAdsFailure (INVALID_SHA256_FORMAT) par index d'operation."""
    from google.rpc import status_pb2

    failure = client.get_type("GoogleAdsFailure")
    for index in indexes:
        err = client.get_type("GoogleAdsError")
        err.message = f"Invalid SHA-256 format (operation {index})"
        err.error_code.offline_user_data_job_error = 25  # INVALID_SHA256_FORMAT
        element = client.get_type("ErrorLocation").FieldPathElement()
        element.field_name = "operations"
        element.index = index
        err.location.field_path_elements.append(element)
        failure.errors.append(err)
    status = status_pb2.Status(code=3, message="partial failure")
    detail = status.details.add()
    detail.type_url = "type.googleapis.com/google.ads.googleads.errors.GoogleAdsFailure"
    detail.value = type(failure).serialize(failure)
    return status


def test_customer_match_ops(client) -> None:
    # Liste Customer Match (CrmBasedUserList, cle CONTACT_INFO).
    op = main._build_user_list_op(client, "Clients 2025", "Acheteurs", 540)
    ul = op.create
    check(op._pb.WhichOneof("operation") == "create", "operation create")
    check(ul._pb.WhichOneof("user_list") == "crm_based_user_list", "oneof crm_based_user_list")
    check(ul.name == "Clients 2025" and ul.description == "Acheteurs", "nom / description")
    check(ul.membership_status.name == "OPEN", "membership_status OPEN")
    check(ul.membership_life_span == 540, "membership_life_span 540")
    check(ul.crm_based_user_list.upload_key_type.name == "CONTACT_INFO", "upload_key_type")
    check(ul.crm_based_user_list.data_source_type.name == "FIRST_PARTY", "data_source_type")
    request = client.get_type("MutateUserListsRequest")
    request.customer_id = CID
    request.operations.append(op)
    request.validate_only = True
    check(len(request.operations) == 1 and len(request._pb.SerializeToString()) > 20, "requete user list")
    op = main._build_user_list_op(client, "Sans description", "", 30)
    check(not op.create.description and op.create.membership_life_span == 30, "description vide ignoree")
    for name in ("CONTACT_INFO", "CRM_ID", "MOBILE_ADVERTISING_ID"):
        getattr(client.enums.CustomerMatchUploadKeyTypeEnum, name)

    # Job OfflineUserDataJob + consentement au niveau du job.
    user_list_rn = f"customers/{CID}/userLists/4242"
    job = main._build_offline_user_data_job(client, user_list_rn)
    check(job.type_.name == "CUSTOMER_MATCH_USER_LIST", "type du job")
    check(job._pb.WhichOneof("metadata") == "customer_match_user_list_metadata", "oneof metadata")
    meta = job.customer_match_user_list_metadata
    check(meta.user_list == user_list_rn, "user_list du job")
    check(job._pb.customer_match_user_list_metadata.HasField("consent"), "consent present")
    check(meta.consent.ad_user_data.name == "GRANTED", "consent.ad_user_data GRANTED")
    check(meta.consent.ad_personalization.name == "GRANTED", "consent.ad_personalization GRANTED")
    create_request = client.get_type("CreateOfflineUserDataJobRequest")
    create_request.customer_id = CID
    create_request.job = job
    create_request.enable_match_rate_range_preview = True
    check(create_request.job.customer_match_user_list_metadata.consent.ad_user_data.name == "GRANTED", "consent conserve dans la requete")
    check(create_request.job.type_.name == "CUSTOMER_MATCH_USER_LIST", "type conserve dans la requete")
    run_request = client.get_type("RunOfflineUserDataJobRequest")
    run_request.resource_name = f"customers/{CID}/offlineUserDataJobs/555"
    check(run_request.resource_name.endswith("/555"), "run request")
    check(
        client.get_service("OfflineUserDataJobService").offline_user_data_job_path(CID, "555")
        == f"customers/{CID}/offlineUserDataJobs/555",
        "chemin offline_user_data_job",
    )

    # Identifiants d'un membre complet : email + telephone + adresse.
    prepared = main.prepare_customer_match_members(MEMBERS)
    job_rn = f"customers/{CID}/offlineUserDataJobs/555"
    requests = list(main._build_add_operations_requests(client, job_rn, prepared["members"]))
    check(len(requests) == 1, "1 requete pour 2 membres")
    request = requests[0]
    check(request.resource_name == job_rn and request.enable_partial_failure is True, "resource_name / partial failure")
    check(request._pb.HasField("enable_partial_failure"), "enable_partial_failure explicite")
    check(len(request.operations) == 2, "2 operations (2 membres exploitables, 1 doublon)")
    check([o._pb.WhichOneof("operation") for o in request.operations] == ["create", "create"], "operations create")
    email_only, full = request.operations[0].create, request.operations[1].create
    check([i._pb.WhichOneof("identifier") for i in email_only.user_identifiers] == ["hashed_email"], "membre email seul : 1 identifiant")
    check(email_only.user_identifiers[0].hashed_email == sha("foobar@gmail.com"), "hash email gmail sans points")
    check(
        [i._pb.WhichOneof("identifier") for i in full.user_identifiers]
        == ["hashed_email", "hashed_phone_number", "address_info"],
        "membre complet : email + telephone + adresse",
    )
    check(full.user_identifiers[0].hashed_email == sha("jean.tremblay@example.com"), "hash email (points conserves hors gmail)")
    check(full.user_identifiers[1].hashed_phone_number == sha("+15145550199"), "hash telephone E.164")
    address = full.user_identifiers[2].address_info
    check(address.hashed_first_name == sha("jean") and address.hashed_last_name == sha("tremblay"), "hash prenom / nom")
    check(address.country_code == "CA" and address.postal_code == "H2X 1Y4", "pays / code postal en clair")
    check(not full._pb.HasField("consent"), "pas de consentement par membre (celui du job s'applique)")

    # remove=True : operations `remove`.
    requests = list(main._build_add_operations_requests(client, job_rn, prepared["members"], remove=True))
    check([o._pb.WhichOneof("operation") for o in requests[0].operations] == ["remove", "remove"], "operations remove")
    check(requests[0].operations[0].remove.user_identifiers[0].hashed_email == sha("foobar@gmail.com"), "remove : memes identifiants")

    # Lots : 2500 membres -> 3 requetes (1000 + 1000 + 500), generateur.
    many = [{"hashed_email": sha(f"user{i}@example.com")} for i in range(2500)]
    gen = main._build_add_operations_requests(client, job_rn, many)
    check(not isinstance(gen, list), "construction paresseuse (generateur)")
    sizes = [len(r.operations) for r in gen]
    check(sizes == [1000, 1000, 500], f"lots : {sizes}")
    check(main.CUSTOMER_MATCH_OPS_PER_REQUEST == 1000, "1000 operations par requete")

    # Erreurs partielles : GoogleAdsFailure dans google.rpc.Status.
    status = make_partial_failure_status(client, [3, 7])
    summary = main.summarize_partial_failure(client, status)
    check(summary["failed_operations"] == 2, f"2 operations en echec : {summary}")
    check(summary["errors"][0]["operation_index"] == 3 and "INVALID_SHA256_FORMAT" in summary["errors"][0]["code"], f"detail erreur : {summary['errors'][0]}")
    check(main.summarize_partial_failure(client, None) is None, "sans statut")
    response = client.get_type("AddOfflineUserDataJobOperationsResponse")
    check(main.summarize_partial_failure(client, response.partial_failure_error) is None, "reponse sans erreur partielle")


def test_customer_match_normalization() -> None:
    # Emails.
    check(main.normalize_email(" Foo.Bar@Gmail.com ") == "foobar@gmail.com", "gmail : points retires, minuscules")
    check(main.sha256_hex(main.normalize_email(" Foo.Bar@Gmail.com ")) == sha("foobar@gmail.com"), "vecteur gmail")
    check(main.normalize_email("J.Doe@GoogleMail.com") == "jdoe@googlemail.com", "googlemail")
    check(main.normalize_email(" Jean.Doe@Example.com ") == "jean.doe@example.com", "points conserves hors gmail")
    check(main.normalize_email("pas-un-email") is None and main.normalize_email("") is None, "email invalide")
    check(main.normalize_email("a@b") is None and main.normalize_email(None) is None, "email sans domaine")
    check(len(main.sha256_hex("x")) == 64, "sha256 hex 64 caracteres")
    check(main.sha256_hex("foobar@gmail.com") == hashlib.sha256(b"foobar@gmail.com").hexdigest(), "sha256 = hashlib")
    check(main.sha256_hex("jean-françois") == hashlib.sha256("jean-françois".encode("utf-8")).hexdigest(), "sha256 sur l'UTF-8 (accents)")
    check(main.sha256_hex("foobar@gmail.com") == main.sha256_hex("foobar@gmail.com").lower(), "hex minuscules")

    # Telephones (E.164).
    check(main.normalize_phone("(514) 555-0199") == "+15145550199", "nord-americain 10 chiffres -> +1")
    check(main.normalize_phone("1 514 555 0199") == "+15145550199", "11 chiffres commencant par 1")
    check(main.normalize_phone("+1 (514) 555-0199") == "+15145550199", "+ conserve, mise en forme retiree")
    check(main.normalize_phone("+33 6 12 34 56 78") == "+33612345678", "international avec +")
    check(main.normalize_phone("0033612345678") == "+33612345678", "prefixe 00 -> +")
    check(main.normalize_phone("011 33 6 12 34 56 78") == "+33612345678", "prefixe 011 (nord-americain) -> +")
    check(main.normalize_phone("0612345678") is None, "numero national sans indicatif (0 initial) refuse")
    check(main.normalize_phone("12345") is None and main.normalize_phone("") is None, "trop court / vide")
    check(main.normalize_phone("+0123456789") is None, "E.164 ne commence pas par 0")
    check(main.normalize_phone("+" + "1" * 16) is None, "plus de 15 chiffres refuse")
    check(main.normalize_phone("514-555-0199 ext. 12") == "+15145550199", "extension 'ext.' retiree")
    check(main.normalize_phone("+1 514 555 0199 x12") == "+15145550199", "extension 'x' retiree")
    check(main.normalize_phone("(514) 555-0199 poste 305") == "+15145550199", "extension 'poste' retiree")
    check(main.normalize_phone("514-555-0199 #7") == "+15145550199", "extension '#' retiree")
    check(main.normalize_phone("5145550199x") == "+15145550199", "x sans chiffres : pas une extension")

    # Noms, pays, codes postaux.
    check(main.normalize_name("  Jean-François ") == "jean-françois", "nom : trim + minuscules, accents conserves")
    check(main.normalize_country_code(" ca ") == "CA" and main.normalize_country_code("Canada") == "CA", "pays -> ISO alpha-2")
    check(main.normalize_country_code("Quebec") is None and main.normalize_country_code("") is None, "pays invalide")
    check(main.normalize_postal_code(" h2x 1y4 ") == "H2X 1Y4", "code postal : trim + majuscules")

    # Membres.
    info = main.normalize_member({"email": " Foo.Bar@Gmail.com "})
    check(list(info["identifiers"]) == ["hashed_email"] and info["identifiers"]["hashed_email"] == sha("foobar@gmail.com"), "email seul -> 1 identifiant")
    info = main.normalize_member(MEMBERS[1])
    check(list(info["identifiers"]) == ["hashed_email", "hashed_phone_number", "address_info"], "membre complet -> 3 identifiants")
    check(info["identifiers"]["address_info"] == {
        "hashed_first_name": sha("jean"), "hashed_last_name": sha("tremblay"), "country_code": "CA", "postal_code": "H2X 1Y4",
    }, f"address_info : {info['identifiers']['address_info']}")
    check(not info["invalid"] and not info["partial_address"], "membre complet valide")
    info = main.normalize_member({})
    check(not info["identifiers"], "membre vide -> aucun identifiant")
    info = main.normalize_member({"first_name": "Marie", "last_name": "Roy", "country_code": "CA"})
    check(not info["identifiers"] and info["partial_address"], "adresse incomplete -> pas d'identifiant")
    info = main.normalize_member({"email": "invalide", "phone": "12", "country_code": "Quebec", "first_name": "a", "last_name": "b", "postal_code": "x"})
    check(info["invalid"] == ["email", "phone", "country_code"] and not info["identifiers"], f"valeurs invalides : {info}")
    info = main.normalize_member({"Email": "foobar@gmail.com", "Phone": "(514) 555-0199", "First Name": "Jean", "Last Name": "Tremblay", "Country": "CA", "Zip": "H2X 1Y4"})
    check(list(info["identifiers"]) == ["hashed_email", "hashed_phone_number", "address_info"], "en-tetes du gabarit Google acceptes")
    info = main.normalize_member({"email": "  ", "phone": None})
    check(not info["identifiers"] and not info["invalid"], "valeurs vides = absentes")

    # Liste complete : comptes, doublons, membres ignores.
    prepared = main.prepare_customer_match_members(MEMBERS)
    counts = prepared["counts"]
    check(counts["members_received"] == 5 and counts["members_to_upload"] == 2, f"comptes : {counts}")
    check(counts["members_skipped_no_identifier"] == 2 and prepared["skipped_positions"] == [3, 4], "membres ignores (positions 1-based)")
    check(counts["members_skipped_duplicate"] == 1, "doublon exact (gabarit Google) envoye une fois")
    check(counts["identifiers"] == {"hashed_email": 2, "hashed_phone_number": 1, "address_info": 1}, f"identifiants par type : {counts['identifiers']}")
    check(counts["members_with_incomplete_address"] == 1 and counts["invalid_values"] == {}, "adresse incomplete comptee")
    check(counts["requests_needed"] == 1, "1 requete")
    check(main.prepare_customer_match_members([{"email": f"u{i}@x.com"} for i in range(2500)])["counts"]["requests_needed"] == 3, "2500 membres -> 3 requetes")

    def expect_error(members, fragment: str) -> None:
        try:
            main.prepare_customer_match_members(members)
        except main.SpecError as ex:
            check(fragment in str(ex), f"message inattendu pour {fragment!r} : {ex}")
        else:
            raise AssertionError(f"SpecError attendue ({fragment!r})")

    expect_error([], "liste non vide")
    expect_error("x", "liste non vide")
    expect_error(["jean@example.com"], "members[1]")
    expect_error([{"email": "a@b.co"}] * (main.CUSTOMER_MATCH_MAX_MEMBERS + 1), "maximum")

    # Resource name d'un job.
    check(main._job_resource_name(CID, "555") == f"customers/{CID}/offlineUserDataJobs/555", "id numerique -> resource name")
    check(main._job_resource_name(CID, f" customers/{CID}/offlineUserDataJobs/555 ") == f"customers/{CID}/offlineUserDataJobs/555", "resource name accepte")
    for bad in ("customers/9999999999/offlineUserDataJobs/1", "abc", ""):
        try:
            main._job_resource_name(CID, bad)
        except main.SpecError:
            pass
        else:
            raise AssertionError(f"SpecError attendue pour {bad!r}")


def test_validation_errors() -> None:
    def expect_error(spec, fragment: str, validator=main.validate_search_campaign_spec) -> None:
        try:
            validator(spec)
        except main.SpecError as ex:
            check(fragment in str(ex), f"message inattendu pour {fragment!r} : {ex}")
        else:
            raise AssertionError(f"SpecError attendue ({fragment!r})")

    def with_ad_group(**changes):
        ag = dict(SAMPLE_SPEC["ad_groups"][0], **changes)
        return dict(SAMPLE_SPEC, ad_groups=[ag])

    expect_error(dict(SAMPLE_SPEC, name=""), "spec.name")
    expect_error(dict(SAMPLE_SPEC, daily_budget=0), "daily_budget")
    expect_error(dict(SAMPLE_SPEC, status="ACTIVE"), "PAUSED ou ENABLED")
    expect_error(dict(SAMPLE_SPEC, bidding={"type": "MANUAL_CPC"}), "bidding.type")
    expect_error(dict(SAMPLE_SPEC, bidding={"type": "TARGET_CPA"}), "target_cpa est requis")
    expect_error(dict(SAMPLE_SPEC, bidding={"type": "MAXIMIZE_CONVERSIONS", "target_roas": 2}), "target_roas")
    expect_error(dict(SAMPLE_SPEC, geo_target_constant_ids=[]), "geo_target_constant_ids")
    expect_error(dict(SAMPLE_SPEC, negative_keywords=[{"text": "x"}]), "match_type")
    expect_error(dict(SAMPLE_SPEC, ad_groups=[]), "ad_groups")
    expect_error(with_ad_group(headlines=["a" * 31, "b", "c"]), "maximum 30")
    expect_error(with_ad_group(headlines=["un", "deux"]), "attendu entre 3 et 15")
    expect_error(with_ad_group(descriptions=["seule"]), "attendu entre 2 et 4")
    expect_error(with_ad_group(descriptions=["d" * 91, "ok"]), "maximum 90")
    expect_error(with_ad_group(headlines=["Meme", "Meme", "Autre"]), "doublon")
    expect_error(with_ad_group(keywords=[{"text": "x", "match_type": "NEAR"}]), "EXACT, PHRASE ou BROAD")
    expect_error(with_ad_group(keywords=[]), "au moins un mot-cle")
    expect_error(with_ad_group(final_url="example.com"), "http://")
    expect_error(with_ad_group(path1="", path2="quebec"), "path2 necessite path1")
    expect_error(with_ad_group(path1="p" * 16), "maximum 15")
    expect_error(dict(SAMPLE_SPEC, ad_groups=[SAMPLE_SPEC["ad_groups"][0]] * 2), "meme nom")
    expect_error(dict(SAMPLE_SPEC, sitelinks=[{"text": "x" * 26, "final_url": "https://a.b"}]), "maximum 25")
    expect_error(dict(SAMPLE_SPEC, sitelinks=[{"text": "x", "final_url": "https://a.b", "description1": "seule"}]), "vont ensemble")
    expect_error(dict(SAMPLE_SPEC, callouts=["c" * 26]), "maximum 25")
    expect_error({}, "spec.name", main.validate_ad_group_spec)
    # Normalisation : doublons de mots-cles ignores, statut en minuscules accepte.
    spec = main.validate_search_campaign_spec(
        dict(SAMPLE_SPEC, status="enabled", negative_keywords=[{"text": "a", "match_type": "exact"}] * 2)
    )
    check(spec["status"] == "ENABLED" and len(spec["negative_keywords"]) == 1, "normalisation")


# ---------------------------------------------------------------------------
# 2) Outils MCP de bout en bout, API remplacee par des doublures
# ---------------------------------------------------------------------------

CAMPAIGN_ROW = {
    "campaign": {
        "id": "42",
        "name": "Campagne test",
        "status": "ENABLED",
        "advertising_channel_type": "SEARCH",
        "bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE",
        "maximize_conversion_value": {"target_roas": 2.5},
        "asset_automation_settings": [
            {"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_OUT"}
        ],
    }
}
ASSET_GROUP_ROW = {
    "campaign": {"id": "42", "name": "Campagne test", "advertising_channel_type": "PERFORMANCE_MAX"},
    "asset_group": {
        "id": "777", "name": "Groupe A", "status": "ENABLED",
        "final_urls": ["https://example.com"], "path1": "chalets", "ad_strength": "GOOD",
    },
}
SIGNAL_ROWS = [
    {"asset_group": {"id": "777"}, "asset_group_signal": {"search_theme": {"text": "chalet a louer"}, "approval_status": "APPROVED"}},
    {"asset_group": {"id": "777"}, "asset_group_signal": {"audience": {"audience": f"customers/{CID}/audiences/9"}}},
]
GOAL_ROWS = [
    {"campaign_conversion_goal": {"category": "PURCHASE", "origin": "WEBSITE"}},  # biddable absent = False
    {"campaign_conversion_goal": {"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": True}},
    {"campaign_conversion_goal": {"category": "PAGE_VIEW", "origin": "WEBSITE", "biddable": True}},
    {"campaign_conversion_goal": {"category": "PURCHASE", "origin": "APP", "biddable": True}},
]
# Lignes user_list telles que MessageToDict les produit (int64 -> chaines).
USER_LIST_ROW = {
    "user_list": {
        "resource_name": f"customers/{CID}/userLists/4242", "id": "4242", "name": "Clients 2025",
        "description": "Acheteurs", "type": "CRM_BASED", "membership_status": "OPEN",
        "membership_life_span": "540", "size_for_search": "1200", "size_for_display": "1500",
        "size_range_for_search": "ONE_THOUSAND_TO_TEN_THOUSAND", "eligible_for_search": True,
        "eligible_for_display": True, "match_rate_percentage": "63",
        "crm_based_user_list": {"upload_key_type": "CONTACT_INFO"},
    }
}
REMARKETING_LIST_ROW = {
    "user_list": {
        "resource_name": f"customers/{CID}/userLists/7", "id": "7", "name": "Visiteurs 30 j",
        "type": "REMARKETING", "membership_status": "OPEN", "eligible_for_display": True,
    }
}
CLOSED_LIST_ROW = {
    "user_list": {
        "resource_name": f"customers/{CID}/userLists/8", "id": "8", "name": "Ancienne liste",
        "type": "CRM_BASED", "membership_status": "CLOSED",
        "crm_based_user_list": {"upload_key_type": "CONTACT_INFO"},
    }
}
JOB_ROW = {
    "offline_user_data_job": {
        "resource_name": f"customers/{CID}/offlineUserDataJobs/555", "id": "555",
        "type": "CUSTOMER_MATCH_USER_LIST", "status": "SUCCESS",
        "customer_match_user_list_metadata": {"user_list": f"customers/{CID}/userLists/4242"},
        "operation_metadata": {"match_rate_range": "MATCH_RANGE_61_TO_70"},
    }
}
QUERY_LOG: list[str] = []
QUERY_CIDS: list[str] = []  # compte de chaque requete GAQL (compte par defaut)

# Campagne Performance Max source (id 77) pour le clonage.
PMAX_CAMPAIGN_ROW = {
    "campaign": {
        "id": "77", "name": "PM-QC-Source", "status": "ENABLED",
        "advertising_channel_type": "PERFORMANCE_MAX",
        "shopping_setting": {"merchant_id": "123456789", "feed_label": "CA", "enable_local": True},
        "brand_guidelines_enabled": True,
        "geo_target_type_setting": {"positive_geo_target_type": "PRESENCE_OR_INTEREST"},
    }
}


def _asset(asset_id: str, **fields) -> dict:
    return dict({"resource_name": f"customers/{CID}/assets/{asset_id}"}, **fields)


def _img(asset_id: str, w: int, h: int) -> dict:
    return _asset(asset_id, image_asset={"full_size": {"width_pixels": str(w), "height_pixels": str(h)}})


PMAX_CAMPAIGN_ASSET_ROWS = [
    {"campaign_asset": {"field_type": "LOGO", "status": "ENABLED"}, "asset": _img("31", 32, 32)},  # trop petit
    {"campaign_asset": {"field_type": "LOGO", "status": "ENABLED"}, "asset": _img("32", 2283, 2283)},
    {"campaign_asset": {"field_type": "LANDSCAPE_LOGO", "status": "ENABLED"}, "asset": _img("33", 2283, 571)},
    {"campaign_asset": {"field_type": "SITELINK", "status": "ENABLED"}, "asset": _asset("34")},
    {"campaign_asset": {"field_type": "BUSINESS_NAME", "status": "ENABLED"}, "asset": _asset("35", text_asset={"text": "Marque test"})},
]
PMAX_GROUP_ROWS = [
    {"asset_group": {"id": "770", "name": "Ancien groupe", "status": "REMOVED", "final_urls": ["https://example.com/old"]}},
    {"asset_group": {"id": "778", "name": "Groupe source", "status": "ENABLED", "final_urls": ["https://example.com/fr/studio"]}},
]
PMAX_GROUP_ASSET_ROWS = [
    {"asset_group_asset": {"field_type": "HEADLINE", "status": "ENABLED", "source": "ADVERTISER"}, "asset": _asset("41", text_asset={"text": "Fabrique au Quebec"})},
    {"asset_group_asset": {"field_type": "MARKETING_IMAGE", "status": "ENABLED", "source": "ADVERTISER"}, "asset": _img("42", 1792, 938)},
    {"asset_group_asset": {"field_type": "MARKETING_IMAGE", "status": "ENABLED", "source": "AUTOMATICALLY_CREATED"}, "asset": _img("43", 1200, 628)},
    {"asset_group_asset": {"field_type": "SQUARE_MARKETING_IMAGE", "status": "ENABLED", "source": "ADVERTISER"}, "asset": _img("44", 1920, 1920)},
    {"asset_group_asset": {"field_type": "SQUARE_MARKETING_IMAGE", "status": "ENABLED", "source": "ADVERTISER"}, "asset": _img("45", 2400, 2400)},
    {"asset_group_asset": {"field_type": "PORTRAIT_MARKETING_IMAGE", "status": "ENABLED", "source": "ADVERTISER"}, "asset": _img("46", 1792, 2232)},
    {"asset_group_asset": {"field_type": "YOUTUBE_VIDEO", "status": "ENABLED", "source": "ADVERTISER"}, "asset": _asset("47", youtube_video_asset={"youtube_video_id": "abc"})},
    {"asset_group_asset": {"field_type": "AD_IMAGE", "status": "ENABLED", "source": "ADVERTISER"}, "asset": _img("48", 533, 533)},
]
PMAX_FILTER_ROWS = [
    {"asset_group_listing_group_filter": {"id": "1", "type": "SUBDIVISION"}},
    {"asset_group_listing_group_filter": {"id": "2", "type": "UNIT_INCLUDED", "case_value": {"product_item_id": {"value": "shopify_ca_1000000000001_2000000000001"}}}},
    {"asset_group_listing_group_filter": {"id": "3", "type": "UNIT_INCLUDED", "case_value": {"product_item_id": {"value": "shopify_ca_1000000000002_2000000000002"}}}},
    {"asset_group_listing_group_filter": {"id": "4", "type": "UNIT_EXCLUDED", "case_value": {"product_item_id": {}}}},
]
PMAX_SIGNAL_ROWS = [
    {"asset_group_signal": {"search_theme": {"text": "luminaire fait au Quebec"}}},
    {"asset_group_signal": {"audience": {"audience": f"customers/{CID}/audiences/5550001"}}},
]


def fake_run_query(customer_id: str, query: str, limit: int = 200) -> list[dict]:
    q = " ".join(query.split())
    QUERY_LOG.append(q)
    QUERY_CIDS.append(customer_id)
    # Campagne Performance Max source (clonage) : branches testees en premier
    # car "FROM asset_group" est un prefixe des autres tables asset_group_*.
    if "FROM campaign WHERE campaign.id = 77" in q:
        return [PMAX_CAMPAIGN_ROW]
    if "FROM campaign_asset WHERE campaign.id = 77" in q:
        return PMAX_CAMPAIGN_ASSET_ROWS
    if "FROM asset_group WHERE campaign.id = 77" in q:
        return PMAX_GROUP_ROWS
    if "FROM asset_group_asset WHERE asset_group.id = 778" in q:
        return PMAX_GROUP_ASSET_ROWS
    if "FROM asset_group_listing_group_filter WHERE asset_group.id = 778" in q:
        return PMAX_FILTER_ROWS
    if "FROM asset_group_signal WHERE asset_group.id = 778" in q:
        return PMAX_SIGNAL_ROWS
    if "FROM campaign WHERE campaign.id = 78" in q:
        return [dict(PMAX_CAMPAIGN_ROW, campaign=dict(PMAX_CAMPAIGN_ROW["campaign"], id="78"))]
    if "FROM campaign_asset WHERE campaign.id = 78" in q:
        return []
    if "FROM asset_group WHERE campaign.id = 78" in q:
        return []
    if "FROM offline_user_data_job" in q:
        if "offlineUserDataJobs/555'" in q or "userLists/4242'" in q:
            return [JOB_ROW]
        return []
    if "FROM user_list" in q:
        if "WHERE user_list.id = " in q:
            wanted = q.split("WHERE user_list.id = ")[1].split()[0]
            return [r for r in (USER_LIST_ROW, REMARKETING_LIST_ROW, CLOSED_LIST_ROW) if r["user_list"]["id"] == wanted]
        return [USER_LIST_ROW, REMARKETING_LIST_ROW, CLOSED_LIST_ROW]
    if "FROM conversion_goal_campaign_config" in q:
        return [dict(CAMPAIGN_ROW, conversion_goal_campaign_config={"goal_config_level": "CUSTOMER"})]
    if "FROM campaign_conversion_goal" in q:
        return GOAL_ROWS
    if "FROM customer_conversion_goal" in q:
        return [{"customer_conversion_goal": {"category": "PURCHASE", "origin": "WEBSITE", "biddable": True}}]
    if "FROM asset_group_signal" in q:
        return SIGNAL_ROWS
    if "FROM asset_group" in q:
        return [ASSET_GROUP_ROW] if "999" not in q else []
    if "FROM audience" in q:
        return [{"audience": {"resource_name": f"customers/{CID}/audiences/9", "name": "Visiteurs 30 j"}}]
    if "FROM campaign WHERE campaign.id = 42" in q:
        return [CAMPAIGN_ROW]
    return []


OFFLINE_JOB_METHODS = (
    "create_offline_user_data_job", "add_offline_user_data_job_operations", "run_offline_user_data_job",
    "suggest_brands",
)
# Marques renvoyees par la doublure de BrandSuggestionService.suggest_brands.
SUGGESTED_BRANDS: list[dict] = []
# Exceptions a lever par la prochaine methode mutate* (consommees dans l'ordre).
MUTATE_FAILURES: list = []
# Statuts d'erreur partielle a renvoyer par add_offline_user_data_job_operations
# (consommes dans l'ordre ; vide = aucune erreur).
PARTIAL_FAILURES: list = []


class RecordingService:
    """Delegue tout au vrai service sauf les methodes mutate* et celles
    d'OfflineUserDataJobService, enregistrees."""

    def __init__(self, real, name: str, log: list, client):
        self._real, self._name, self._log, self._client = real, name, log, client

    def __getattr__(self, attr):
        if not attr.startswith("mutate") and attr not in OFFLINE_JOB_METHODS:
            return getattr(self._real, attr)

        def _mutate(request=None, **kwargs):
            self._log.append((self._name, attr, request))
            if MUTATE_FAILURES and attr.startswith("mutate"):
                raise MUTATE_FAILURES.pop(0)
            if attr == "create_offline_user_data_job":
                return SimpleNamespace(resource_name=f"customers/{CID}/offlineUserDataJobs/555")
            if attr == "add_offline_user_data_job_operations":
                response = self._client.get_type("AddOfflineUserDataJobOperationsResponse")
                if PARTIAL_FAILURES:
                    response.partial_failure_error = PARTIAL_FAILURES.pop(0)
                return response
            if attr == "suggest_brands":
                response = self._client.get_type("SuggestBrandsResponse")
                for b in SUGGESTED_BRANDS:
                    suggestion = self._client.get_type("BrandSuggestion")
                    suggestion.id, suggestion.name = b["id"], b["name"]
                    suggestion.urls.extend(b["urls"])
                    suggestion.state = getattr(self._client.enums.BrandStateEnum, b["state"])
                    response.brands.append(suggestion)
                return response
            if attr == "run_offline_user_data_job":
                return SimpleNamespace(operation=SimpleNamespace(name=f"customers/{CID}/operations/abc"))
            if attr == "mutate":  # GoogleAdsService.mutate
                response = self._client.get_type("MutateGoogleAdsResponse")
                if not request.validate_only:
                    for i, op in enumerate(request.mutate_operations, 1):
                        kind = op._pb.WhichOneof("operation")
                        result = self._client.get_type("MutateOperationResponse")
                        getattr(result, kind.replace("_operation", "_result")).resource_name = (
                            f"customers/{CID}/{kind.removesuffix('_operation')}/{100 + i}"
                        )
                        response.mutate_operation_responses.append(result)
                return response
            n = 0 if request.validate_only else len(request.operations)
            if attr == "mutate_campaign_criteria":
                return SimpleNamespace(results=[
                    SimpleNamespace(resource_name=f"customers/{CID}/campaignCriteria/{op.create.campaign.rsplit('/', 1)[-1]}~{700 + i}")
                    for i, op in enumerate(request.operations[:n])
                ])
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name=f"customers/{CID}/{self._name}/{i}") for i in range(n)]
            )

        return _mutate


class RecordingClient:
    def __init__(self, real):
        self._real, self.log = real, []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get_service(self, name, *args, **kwargs):
        return RecordingService(self._real.get_service(name, *args, **kwargs), name, self.log, self._real)


def test_tools_end_to_end(default_client) -> None:
    client = RecordingClient(default_client)
    main.get_client = lambda: client
    main.run_query = fake_run_query

    def call(tool, *args, **kwargs) -> dict:
        return json.loads(tool(*args, **kwargs))

    QUERY_CIDS.clear()
    # customer_id facultatif : explicite (tirets acceptes) ou compte non servi.
    res = call(main.list_asset_groups, "42", customer_id="123-456-7890")
    check(res["asset_group_count"] == 1 and QUERY_CIDS[-1] == CID, f"compte explicite : {res}")
    res = call(main.create_search_campaign, SAMPLE_SPEC, dry_run=True, customer_id="111-222-3333")
    check("n'est pas servi" in res["error"]["message"] and not client.log, f"compte non servi : {res}")

    # Lecture
    res = call(main.list_asset_groups, "42")
    check(res["asset_group_count"] == 1 and res["asset_groups"][0]["id"] == "777", f"list_asset_groups : {res}")
    check(res["asset_groups"][0]["search_themes"][0]["text"] == "chalet a louer", "themes regroupes")
    check(res["asset_groups"][0]["audience_signals"][0]["name"] == "Visiteurs 30 j", "nom d'audience resolu")
    res = call(main.get_campaign_conversion_goals, "42")
    check(res["goal_config_level"] == "CUSTOMER" and len(res["campaign_goals"]) == 4, f"goals : {res}")
    check(res["campaign_biddable_categories"] == ["ADD_TO_CART", "PAGE_VIEW", "PURCHASE"], "categories biddable")
    check(res["account_biddable_categories"] == ["PURCHASE"], "categories compte")

    # set_campaign_target_roas
    client.log.clear()
    res = call(main.set_campaign_target_roas, "42", 3.0, dry_run=True)
    check(res.get("valid") is True and res["previous_target_roas"] == 2.5, f"target_roas dry run : {res}")
    svc, method, request = client.log[-1]
    check((svc, method) == ("CampaignService", "mutate_campaigns") and request.validate_only, "requete ROAS")
    check(request.operations[0].update.maximize_conversion_value.target_roas == 3.0, "valeur ROAS envoyee")
    res = call(main.set_campaign_target_roas, "42", None)
    check(res["new_target_roas"] is None and res["updated"], f"effacement ROAS : {res}")
    check(not client.log[-1][2].validate_only, "effacement reel (validate_only False)")
    check(json.loads(main.set_campaign_target_roas("42", 300))["error"], "300 refuse (pourcentage)")

    # set_campaign_conversion_goals : 2 requetes separees
    client.log.clear()
    res = call(main.set_campaign_conversion_goals, "42", ["purchase"], dry_run=True)
    check(res.get("valid") is True and res["goal_config_level_changed"] is True, f"goals dry run : {res}")
    check(res["biddable"] == {"ADD_TO_CART": False, "PAGE_VIEW": False, "PURCHASE": True}, f"map biddable : {res['biddable']}")
    check(len(res["changed_goals"]) == 3 and res["unchanged_goals"] == 1, f"changements : {res['changed_goals']}")
    check([(s, m) for s, m, _ in client.log] == [
        ("ConversionGoalCampaignConfigService", "mutate_conversion_goal_campaign_configs"),
        ("CampaignConversionGoalService", "mutate_campaign_conversion_goals"),
    ], f"sequence de requetes : {[(s, m) for s, m, _ in client.log]}")
    check(all(req.validate_only for _, _, req in client.log), "les deux requetes en validate_only")
    check(len(client.log[1][2].operations) == 3, "3 operations d'objectifs")
    res = call(main.set_campaign_conversion_goals, "42", ["SIGNUP"])
    check("SIGNUP" in json.dumps(res["error"]) and "available_categories" in res["error"], f"categorie absente : {res}")
    res = call(main.set_campaign_conversion_goals, "42", ["FOO"])
    check("valid_categories" in res["error"], "categorie inconnue")

    # add_search_themes : doublon existant ignore, requete reelle
    client.log.clear()
    res = call(main.add_search_themes, "777", ["Chalet a louer", "spa nature", "  "])
    check(res["added"] == 1 and res["already_present"] == ["Chalet a louer"], f"themes : {res}")
    svc, method, request = client.log[-1]
    check((svc, method) == ("AssetGroupSignalService", "mutate_asset_group_signals") and not request.validate_only, "requete themes")
    check(request.operations[0].create.search_theme.text == "spa nature", "theme envoye")
    check("introuvable" in json.loads(main.add_search_themes("999", ["x"]))["error"], "groupe inexistant")

    # set_campaign_url_expansion : liste complete reenvoyee
    client.log.clear()
    res = call(main.set_campaign_url_expansion, "42", True, dry_run=True)
    check(res["new_setting"] == "OPTED_OUT" and res["previous_setting"] == "(defaut Google)", f"url expansion : {res}")
    request = client.log[-1][2]
    settings = request.operations[0].update.asset_automation_settings
    check([(s.asset_automation_type.name, s.asset_automation_status.name) for s in settings] == [
        ("TEXT_ASSET_AUTOMATION", "OPTED_OUT"), (main.FINAL_URL_EXPANSION_SETTING, "OPTED_OUT"),
    ], "reglages conserves + nouveau")
    check(request.validate_only and list(request.operations[0].update_mask.paths) == ["asset_automation_settings"], "masque")

    # create_search_campaign : une seule requete GoogleAdsService.mutate
    client.log.clear()
    res = call(main.create_search_campaign, SAMPLE_SPEC, dry_run=True)
    check(res.get("valid") is True and res["operations"] == EXPECTED_COUNTS, f"create dry run : {res}")
    svc, method, request = client.log[-1]
    check((svc, method) == ("GoogleAdsService", "mutate") and len(client.log) == 1, "une seule requete")
    check(request.validate_only and len(request.mutate_operations) == 26, "26 operations, validate_only")
    res = call(main.create_search_campaign, SAMPLE_SPEC)
    check(res["campaign_id"] == "102" and res["created"]["campaign"] == [f"customers/{CID}/campaign/102"], f"create reel : {res}")
    check(sorted(res["created"]) == sorted(EXPECTED_COUNTS) and len(res["created"]["ad_group_criterion"]) == 5, "resource names par type")
    check("PAUSE" in res["note"], "note PAUSE")
    res = call(main.create_search_campaign, dict(SAMPLE_SPEC, daily_budget=-1))
    check("daily_budget" in res["error"], "validation avant appel API")

    # create_pmax_campaign : clonage d'une campagne source, une seule requete
    client.log.clear()
    QUERY_LOG.clear()
    clone_spec = {
        "name": "PM-CA-EN-Clone", "daily_budget": 40, "status": "ENABLED",
        "geo_target_constant_ids": [2124], "language_constant_ids": [1000],
        "negative_keywords": [{"text": "outdoor", "match_type": "BROAD"}],
        "final_url": "https://example.com/en/pages/studio",
        "headlines": PMAX_SPEC["headlines"], "long_headlines": PMAX_SPEC["long_headlines"],
        "descriptions": PMAX_SPEC["descriptions"],
        "search_themes": ["pendant light"], "clone_from_campaign_id": "77",
    }
    res = call(main.create_pmax_campaign, clone_spec, dry_run=True)
    check(res.get("valid") is True and res["source_campaign"] == {"id": "77", "name": "PM-QC-Source", "asset_group_id": "778"}, f"clone dry run : {res}")
    check(res["merchant_id"] == "123456789" and res["feed_label"] == "CA" and res["business_name"] == "Marque test", f"reglages clones : {res}")
    check(res["reused_assets"] == {
        "marketing_image_asset_ids": 1, "square_marketing_image_asset_ids": 2,
        "portrait_marketing_image_asset_ids": 1, "youtube_video_asset_ids": 1,
        "logo_asset_ids": 1, "landscape_logo_asset_ids": 1,
    }, f"assets repris : {res['reused_assets']}")
    check(res["listing_group"] == "2 fiches incluses, le reste exclu" and res["audience_ids"] == ["5550001"], f"fiches / audiences : {res}")
    check(res["cloned_from_source"]["include_item_ids"] == 2 and res["cloned_from_source"]["business_name"] == "Marque test", f"cloned_from_source : {res['cloned_from_source']}")
    check(res["operations"] == {
        "campaign_budget": 1, "campaign": 1, "campaign_asset": 3, "campaign_criterion": 3,
        "asset_group": 1, "asset": 6, "asset_group_asset": 6 + 5, "asset_group_listing_group_filter": 4,
        "asset_group_signal": 2,
    }, f"operations clone : {res['operations']}")
    svc, method, request = client.log[-1]
    check((svc, method) == ("GoogleAdsService", "mutate") and len(client.log) == 1 and request.validate_only, "une requete validate_only")
    logos = [op.campaign_asset_operation.create.asset for op in request.mutate_operations if op._pb.WhichOneof("operation") == "campaign_asset_operation"]
    check(logos == [f"customers/{CID}/assets/35", f"customers/{CID}/assets/32", f"customers/{CID}/assets/33"], f"nom/logos existants (favicon 32 px ecarte) : {logos}")
    reused = [op.asset_group_asset_operation.create.asset for op in request.mutate_operations if op._pb.WhichOneof("operation") == "asset_group_asset_operation"][6:]
    check(reused == [f"customers/{CID}/assets/{i}" for i in ("42", "44", "45", "46", "47")], f"images/videos reprises (auto-generee et AD_IMAGE ecartees) : {reused}")
    check(any("FROM asset_group_listing_group_filter WHERE asset_group.id = 778" in q for q in QUERY_LOG), "lecture du filtre source")
    client.log.clear()
    res = call(main.create_pmax_campaign, clone_spec)
    check(res["campaign_id"] == "102" and res["asset_group_id"] == "115" and res["created"]["asset_group_listing_group_filter"] == 4, f"clone reel : {res}")
    check("ACTIVE" in res["note"] and "set_campaign_conversion_goals" in res["note"] and "ATTENTION" not in res["note"], "note")
    check(res["asset_group_status"] == "ENABLED" and "asset_group_activation_error" not in res, f"groupe active : {res}")
    check([(s, m) for s, m, _ in client.log] == [("GoogleAdsService", "mutate"), ("AssetGroupService", "mutate_asset_groups")], f"creation puis activation : {[(s, m) for s, m, _ in client.log]}")
    activation = client.log[-1][2]
    check(activation.customer_id == CID and len(activation.operations) == 1 and not activation.validate_only, "requete d'activation")
    check(activation.operations[0].update.resource_name == f"customers/{CID}/asset_group/115" and activation.operations[0].update.status.name == "ENABLED", "groupe cree -> ENABLED")
    # Passerelle via create_search_campaign (spec Performance Max).
    client.log.clear()
    res = call(main.create_search_campaign, dict(clone_spec, advertising_channel_type="PERFORMANCE_MAX"), dry_run=True)
    check(res.get("valid") is True and res["source_campaign"]["id"] == "77" and client.log[-1][2].validate_only, f"passerelle PMax : {res}")
    # Refus : source non PMax, source sans groupe d'assets, assets manquants sans clonage.
    client.log.clear()
    res = call(main.create_pmax_campaign, dict(clone_spec, clone_from_campaign_id="42"))
    check("Performance Max" in res["error"] and "SEARCH" in res["error"], f"source Search refusee : {res}")
    res = call(main.create_pmax_campaign, dict(clone_spec, clone_from_campaign_id="78"))
    check("aucun groupe d'assets" in res["error"], f"source vide refusee : {res}")
    res = call(main.create_pmax_campaign, dict(clone_spec, clone_from_campaign_id="999"))
    check("introuvable" in res["error"], "source inexistante")
    no_clone = dict(clone_spec)
    no_clone.pop("clone_from_campaign_id")
    res = call(main.create_pmax_campaign, no_clone)
    check("marketing_image_asset_ids" in res["error"] and "business_name" in res["error"], f"assets manquants : {res}")
    check(not client.log, "refus avant tout appel d'ecriture")
    res = call(main.create_pmax_campaign, dict(clone_spec, headlines=["a"]))
    check("attendu entre 3 et 15" in res["error"], "validation avant lecture de la source")

    # add_search_ad_group / add_campaign_assets
    client.log.clear()
    res = call(main.add_search_ad_group, "42", SAMPLE_SPEC["ad_groups"][1], dry_run=True)
    check(res["operations"] == {"ad_group": 1, "ad_group_ad": 1, "ad_group_criterion": 2}, f"add ad group : {res}")
    request = client.log[-1][2]
    check(request.mutate_operations[0].ad_group_operation.create.campaign == f"customers/{CID}/campaigns/42", "campagne existante")
    res = call(main.add_search_ad_group, "42", {"name": "x"})
    check("final_url" in res["error"], "validation groupe")
    res = call(main.add_campaign_assets, "42", callouts=["Livraison gratuite"])
    check(res["operations"] == {"asset": 1, "campaign_asset": 1} and res["created"]["campaign_asset"], f"add assets : {res}")
    res = call(main.add_campaign_assets, "42")
    check("au moins un sitelink" in res["error"], "assets vides refuses")
    res = call(main.add_campaign_assets, "42", sitelinks=[{"text": "x" * 26, "final_url": "https://a.b"}])
    check("maximum 25" in res["error"], "sitelink trop long")

    # list_user_lists / get_customer_match_status (lecture)
    res = call(main.list_user_lists)
    check(res["user_list_count"] == 3 and [l["name"] for l in res["user_lists"]] == ["Ancienne liste", "Clients 2025", "Visiteurs 30 j"], f"list_user_lists : {res}")
    cm = res["user_lists"][1]
    check(cm["id"] == "4242" and cm["type"] == "CRM_BASED" and cm["size_for_search"] == 1200, f"liste CM : {cm}")
    check(cm["match_rate_percentage"] == 63 and cm["eligible_for_search"] is True and cm["upload_key_type"] == "CONTACT_INFO", "champs CM")
    check(res["user_lists"][2]["size_for_search"] is None and res["user_lists"][2]["upload_key_type"] is None, "champs absents -> None")
    res = call(main.get_customer_match_status, user_list_id="4242")
    check(res["user_list"]["name"] == "Clients 2025" and res["user_list"]["membership_life_span_days"] == 540, f"statut liste : {res}")
    check(res["user_list"]["size_range_for_search"] == "ONE_THOUSAND_TO_TEN_THOUSAND", "size range")
    check(res["recent_jobs"][0]["status"] == "SUCCESS" and res["recent_jobs"][0]["match_rate_range"] == "MATCH_RANGE_61_TO_70", f"jobs recents : {res}")
    check("job" not in res, "pas de section job sans job_resource_name")
    res = call(main.get_customer_match_status, job_resource_name="555")
    check(res["job"]["resource_name"].endswith("/555") and res["job"]["status"] == "SUCCESS" and "user_list" not in res, f"statut job : {res}")
    check(res["job"]["user_list"] == f"customers/{CID}/userLists/4242" and res["job"]["type"] == "CUSTOMER_MATCH_USER_LIST", "job -> liste")
    check("offlineUserDataJobs/555'" in QUERY_LOG[-1] and "offline_user_data_job.failure_reason" in QUERY_LOG[-1], f"requete job : {QUERY_LOG[-1]}")
    res = call(main.get_customer_match_status, user_list_id="4242", job_resource_name=f"customers/{CID}/offlineUserDataJobs/555")
    check("job" in res and "user_list" in res, "les deux sections quand les deux sont fournis")
    check("error" in call(main.get_customer_match_status), "aucun parametre refuse")
    check("introuvable" in call(main.get_customer_match_status, job_resource_name="556")["error"], "job inexistant")
    check("introuvable" in call(main.get_customer_match_status, user_list_id="999")["error"], "liste inexistante")
    check("compte" in call(main.get_customer_match_status, job_resource_name="customers/9999999999/offlineUserDataJobs/1")["error"], "autre compte refuse")

    # create_customer_match_list : doublon refuse, dry run, creation reelle
    client.log.clear()
    res = call(main.create_customer_match_list, "clients 2025")
    check("existe deja" in res["error"]["message"] and res["error"]["existing_id"] == "4242", f"doublon (casse ignoree) : {res}")
    check(res["error"]["existing_resource_name"] == f"customers/{CID}/userLists/4242" and not client.log, "aucune requete d'ecriture")
    res = call(main.create_customer_match_list, "Clients VIP", "Meilleurs clients", 9999, dry_run=True)
    check(res.get("valid") is True and res["membership_life_span_days"] == 540 and "9999" in res["life_span_note"], f"dry run liste : {res}")
    svc, method, request = client.log[-1]
    check((svc, method) == ("UserListService", "mutate_user_lists") and request.validate_only, "requete user list validate_only")
    created = request.operations[0].create
    check(created.name == "Clients VIP" and created.description == "Meilleurs clients", "nom / description envoyes")
    check(created.crm_based_user_list.upload_key_type.name == "CONTACT_INFO" and created.membership_status.name == "OPEN", "CONTACT_INFO / OPEN")
    check(created.membership_life_span == 540, "duree plafonnee a 540")
    res = call(main.create_customer_match_list, "Clients VIP", membership_life_span_days=180)
    check(res["id"] == "0" and res["resource_name"] == f"customers/{CID}/UserListService/0" and res["membership_life_span_days"] == 180, f"creation reelle : {res}")
    check(not client.log[-1][2].validate_only and "life_span_note" not in res, "creation reelle, sans note de plafond")
    check("name" in call(main.create_customer_match_list, "  ")["error"], "nom vide refuse")
    check(">= 1" in call(main.create_customer_match_list, "X", membership_life_span_days=0)["error"], "duree 0 refusee")

    # upload_customer_match_members : dry run = aucun appel API du tout
    client.log.clear()
    QUERY_LOG.clear()
    res = call(main.upload_customer_match_members, "4242", MEMBERS, dry_run=True)
    check(res["dry_run"] is True and res["api_called"] is False and res["valid"] is True, f"upload dry run : {res}")
    check(res["counts"]["members_to_upload"] == 2 and res["counts"]["identifiers"] == {"hashed_email": 2, "hashed_phone_number": 1, "address_info": 1}, f"comptes dry run : {res['counts']}")
    check(res["skipped_member_positions"] == [3, 4] and res["would_send_requests"] == 1, "positions ignorees / requetes")
    check(not client.log and not QUERY_LOG, "dry run : ni requete GAQL ni appel de service")
    check("job_resource_name" not in res, "dry run : pas de job")

    # upload reel : job cree -> operations (lots) -> run
    res = call(main.upload_customer_match_members, "4242", MEMBERS)
    check(res["job_resource_name"] == f"customers/{CID}/offlineUserDataJobs/555" and res["job_id"] == "555", f"upload reel : {res}")
    check(res["requests_sent"] == 1 and res["operations_sent"] == 2 and res["operations_rejected"] == 0 and res["partial_failures"] == [], "1 requete, 2 operations")
    check(res["user_list"]["name"] == "Clients 2025" and res["operation"] == "add" and "48 h" in res["note"], "liste / note")
    check([(s, m) for s, m, _ in client.log] == [
        ("OfflineUserDataJobService", "create_offline_user_data_job"),
        ("OfflineUserDataJobService", "add_offline_user_data_job_operations"),
        ("OfflineUserDataJobService", "run_offline_user_data_job"),
    ], f"sequence : {[(s, m) for s, m, _ in client.log]}")
    create_request, add_request, run_request = (req for _, _, req in client.log)
    check(create_request.customer_id == CID and create_request.enable_match_rate_range_preview is True, "create request")
    check(create_request.job.type_.name == "CUSTOMER_MATCH_USER_LIST", "type du job envoye")
    check(create_request.job.customer_match_user_list_metadata.user_list == f"customers/{CID}/userLists/4242", "liste cible")
    check(create_request.job.customer_match_user_list_metadata.consent.ad_user_data.name == "GRANTED", "consent ad_user_data")
    check(create_request.job.customer_match_user_list_metadata.consent.ad_personalization.name == "GRANTED", "consent ad_personalization")
    check(add_request.resource_name == f"customers/{CID}/offlineUserDataJobs/555" and add_request.enable_partial_failure is True, "add request")
    check(len(add_request.operations) == 2 and add_request.operations[0].create.user_identifiers[0].hashed_email == sha("foobar@gmail.com"), "operations hachees")
    check(len(add_request.operations[1].create.user_identifiers) == 3, "membre complet : 3 identifiants")
    check(run_request.resource_name == f"customers/{CID}/offlineUserDataJobs/555", "run request")
    check("FROM user_list WHERE user_list.id = 4242" in " ".join(QUERY_LOG), "verification prealable de la liste")

    # 2500 membres -> 3 requetes d'operations, remove=True, erreurs partielles
    client.log.clear()
    PARTIAL_FAILURES.append(make_partial_failure_status(client._real, [2, 5]))
    big = [{"email": f"user{i}@example.com"} for i in range(2500)]
    res = call(main.upload_customer_match_members, "4242", big, remove=True)
    methods = [m for _, m, _ in client.log]
    check(methods == ["create_offline_user_data_job"] + ["add_offline_user_data_job_operations"] * 3 + ["run_offline_user_data_job"], f"3 lots : {methods}")
    sizes = [len(req.operations) for _, m, req in client.log if m == "add_offline_user_data_job_operations"]
    check(sizes == [1000, 1000, 500] and res["requests_sent"] == 3 and res["operations_sent"] == 2500, f"tailles des lots : {sizes}")
    check(client.log[1][2].operations[0]._pb.WhichOneof("operation") == "remove" and res["operation"] == "remove", "remove=True -> operations remove")
    check(res["operations_rejected"] == 2 and res["partial_failures"][0]["request_index"] == 1, f"erreurs partielles : {res['partial_failures']}")
    check(res["partial_failures"][0]["errors"][0]["operation_index"] == 2 and not PARTIAL_FAILURES, "detail erreur partielle")

    # Toutes les operations rejetees : le job n'est pas lance, detail remonte.
    client.log.clear()
    PARTIAL_FAILURES.append(make_partial_failure_status(client._real, [0, 1]))
    res = call(main.upload_customer_match_members, "4242", MEMBERS)
    check("rejete toutes" in res["error"]["message"] and res["error"]["partial_failures"][0]["failed_operations"] == 2, f"tout rejete : {res}")
    check([m for _, m, _ in client.log] == ["create_offline_user_data_job", "add_offline_user_data_job_operations"], "pas de run_offline_user_data_job")

    # Refus : liste inexistante, mauvais type, liste fermee, aucun membre exploitable
    client.log.clear()
    check("introuvable" in call(main.upload_customer_match_members, "999", MEMBERS)["error"], "liste inexistante")
    res = call(main.upload_customer_match_members, "7", MEMBERS)
    check("CRM_BASED" in res["error"]["message"] and res["error"]["user_list"]["type"] == "REMARKETING", f"liste remarketing refusee : {res}")
    check("fermee" in call(main.upload_customer_match_members, "8", MEMBERS)["error"]["message"], "liste fermee refusee")
    res = call(main.upload_customer_match_members, "4242", [{}, {"first_name": "x"}])
    check("Aucun membre exploitable" in res["error"]["message"] and res["error"]["counts"]["members_skipped_no_identifier"] == 2, f"aucun membre : {res}")
    check("liste non vide" in call(main.upload_customer_match_members, "4242", [])["error"], "liste vide refusee")
    check("members[1]" in call(main.upload_customer_match_members, "4242", ["a@b.co"])["error"], "membre non objet refuse")
    check(not client.log, "refus avant tout appel de service")

    # Aucun outil n'a reçu customer_id : toutes les requetes GAQL et toutes les
    # ecritures visent le compte par defaut.
    check(QUERY_CIDS and set(QUERY_CIDS) == {CID}, f"comptes interroges : {set(QUERY_CIDS)}")


PMAX_ID = "1000000002"
SEARCH_ID = "1000000003"
VIDEO_ID = "1000000004"
BRAND_SET_RN = f"customers/{CID}/sharedSets/555"
# Etat simule, modifiable par les tests : listes de marques liees a la PMax
# [(resource name, negative)], noms deja pris, campagne App dependante ou non.
BRAND_STATE: dict = {"attached": [], "existing_names": []}
APP_STATE: dict = {"campaign": APP_CAMPAIGN, "config_level": None}


def _gaql_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def account_run_query(customer_id: str, query: str, limit: int = 200) -> list[dict]:
    """run_query simule pour les objectifs du compte et les marques."""
    q = " ".join(query.split())
    QUERY_LOG.append(q)
    if "IN ()" in q:  # Google : PROHIBITED_EMPTY_LIST_IN_CONDITION
        raise AssertionError(f"liste vide dans IN : {q}")
    if "FROM customer_conversion_goal" in q:
        return [{"customer_conversion_goal": dict(g)} for g in ACCOUNT_GOALS]
    if "FROM conversion_goal_campaign_config" in q:
        # La campagne App n'a pas de ligne ici (comme dans un vrai compte).
        return [
            {"campaign": {"id": SEARCH_ID, "name": "Recherche - marque", "status": "ENABLED",
                          "advertising_channel_type": "SEARCH", "bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE"},
             "conversion_goal_campaign_config": {"goal_config_level": "CUSTOMER"}},
            {"campaign": {"id": PMAX_ID, "name": "PMax - boutique", "status": "ENABLED",
                          "advertising_channel_type": "PERFORMANCE_MAX", "bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE"},
             "conversion_goal_campaign_config": {"goal_config_level": "CAMPAIGN"}},
            {"campaign": {"id": VIDEO_ID, "name": "Video", "status": "ENABLED",
                          "advertising_channel_type": "VIDEO", "bidding_strategy_type": "TARGET_CPM"},
             "conversion_goal_campaign_config": {"goal_config_level": "CUSTOMER"}},
        ] + ([{"campaign": {"id": APP_ID, "name": "App - installations", "status": "ENABLED",
                            "advertising_channel_type": "MULTI_CHANNEL", "bidding_strategy_type": "TARGET_CPA"},
               "conversion_goal_campaign_config": {"goal_config_level": APP_STATE["config_level"]}}]
             if APP_STATE["config_level"] else [])
    if "advertising_channel_type = 'MULTI_CHANNEL'" in q:
        return [{"campaign": APP_STATE["campaign"]}]
    if "FROM conversion_action WHERE conversion_action.resource_name IN" in q:
        return [{"conversion_action": dict(a, resource_name=rn)} for rn, a in APP_ACTIONS.items() if f"'{rn}'" in q]
    if "FROM shared_set WHERE shared_set.type = 'BRANDS' AND shared_set.name" in q:
        return [{"shared_set": {"id": "555", "name": n, "status": "ENABLED"}}
                for n in BRAND_STATE["existing_names"] if _gaql_quote(n) in q]
    if "FROM shared_set WHERE shared_set.id = 555" in q:
        return [{"shared_set": {"id": "555", "name": "Marque", "type_": "BRANDS", "status": "ENABLED", "member_count": "1"}}]
    if "FROM shared_set WHERE shared_set.id = 556" in q:
        return [{"shared_set": {"id": "556", "name": "Negatifs", "type_": "NEGATIVE_KEYWORDS", "status": "ENABLED"}}]
    if "FROM shared_set WHERE shared_set.id = 557" in q:
        return [{"shared_set": {"id": "557", "name": "Ancienne", "type_": "BRANDS", "status": "REMOVED"}}]
    if "FROM shared_criterion WHERE shared_set.id = 555" in q:
        return [{"shared_criterion": {"brand": {"entity_id": "b-1", "display_name": "Marque test", "status": "ENABLED"}}}]
    if f"FROM campaign_criterion WHERE campaign.id = {PMAX_ID}" in q and "BRAND_LIST" in q:
        return [{"campaign_criterion": {"criterion_id": "9", "negative": neg, "brand_list": {"shared_set": rn}}}
                for rn, neg in BRAND_STATE["attached"]]
    if f"FROM campaign WHERE campaign.id = {PMAX_ID}" in q:
        return [{"campaign": {"id": PMAX_ID, "name": "PMax - boutique", "status": "ENABLED",
                              "advertising_channel_type": "PERFORMANCE_MAX"}}]
    if f"FROM campaign WHERE campaign.id = {SEARCH_ID}" in q:
        return [{"campaign": {"id": SEARCH_ID, "name": "Recherche - marque", "status": "ENABLED",
                              "advertising_channel_type": "SEARCH"}}]
    return []


def make_google_ads_exception(client, *op_indexes: int):
    """GoogleAdsException hors ligne : une erreur sur operations[i].biddable par index."""
    from google.ads.googleads.errors import GoogleAdsException

    failure = client.get_type("GoogleAdsFailure")
    for op_index in op_indexes:
        error = client.get_type("GoogleAdsError")
        error.message = f"Objectif {op_index} non modifiable."
        error.error_code.field_error = client.get_type("FieldErrorEnum").FieldError.IMMUTABLE_FIELD
        for name, index in (("operations", op_index), ("update", None), ("biddable", None)):
            element = type(error.location).FieldPathElement()
            element.field_name = name
            if index is not None:
                element.index = index
            error.location.field_path_elements.append(element)
        failure.errors.append(error)
    return GoogleAdsException(None, None, failure, "req-test")


def test_goal_and_brand_tools_end_to_end(default_client) -> None:
    client = RecordingClient(default_client)
    main.get_client = lambda: client
    main.run_query = account_run_query

    def call(tool, *args, **kwargs) -> dict:
        return json.loads(tool(*args, **kwargs))

    def last_queries(fragment: str) -> list[str]:
        return [q for q in QUERY_LOG if fragment in q]

    # --- set_customer_conversion_goals : dry_run par defaut, plan de l'etape 1
    client.log.clear()
    QUERY_LOG.clear()
    res = call(main.set_customer_conversion_goals, PLAN_GOALS)
    check(res.get("dry_run") is True and res.get("valid") is True, f"dry run par defaut : {res}")
    check(res["after_biddable"] == ["PURCHASE/WEBSITE"], f"apres : {res['after_biddable']}")
    check(len(res["before_biddable"]) == 10 and len(res["changes"]) == 9, "avant / changements")
    check(res["unchanged_requested"] == len(PLAN_GOALS) - 9, f"inchanges : {res['unchanged_requested']}")
    check([c["id"] for c in res["campaigns_following_account_goals"]] == [SEARCH_ID, VIDEO_ID], "campagnes heritieres")
    check([c["id"] for c in res["campaigns_with_own_goals"]] == [PMAX_ID], "PMax a ses propres objectifs")
    app = res["app_campaigns"][0]
    check(app["id"] == APP_ID and app["depends_on_account_goals"] is False and res["warnings"] == [], f"App : {app}")
    check(any("independante" in n for n in res["notices"]), f"avis App : {res['notices']}")
    check([(sv, m) for sv, m, _ in client.log] == [("CustomerConversionGoalService", "mutate_customer_conversion_goals")], f"requetes : {client.log}")
    request = client.log[0][2]
    check(request.validate_only and len(request.operations) == 9, "9 operations en validate_only")
    check(all(op.update.biddable is False for op in request.operations), "toutes en secondaire")
    # Filtres GAQL : campagnes supprimees exclues.
    check(all("campaign.status != 'REMOVED'" in q for q in last_queries("FROM conversion_goal_campaign_config")), "config : REMOVED exclu")
    check(all("campaign.status != 'REMOVED'" in q for q in last_queries("'MULTI_CHANNEL'")), "App : REMOVED exclu")
    check(any(f"'{APP_ACTION_RN}'" in q for q in last_queries("FROM conversion_action")), "actions App lues")
    # Doublons : comptes une seule fois.
    res = call(main.set_customer_conversion_goals, [
        {"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": False},
        {"category": "add_to_cart", "origin": "website", "biddable": "false"},
        {"category": "PURCHASE", "origin": "WEBSITE", "biddable": True},
    ])
    check(len(res["changes"]) == 1 and res["unchanged_requested"] == 1, f"doublons : {res['unchanged_requested']}")
    # Rien a changer : aucun appel a l'API.
    client.log.clear()
    res = call(main.set_customer_conversion_goals, [{"category": "PURCHASE", "origin": "WEBSITE", "biddable": True}], dry_run=False)
    check(res["updated_goals"] == 0 and not client.log, f"aucun changement : {res}")
    # Application reelle, plusieurs changements dans les deux sens.
    client.log.clear()
    res = call(main.set_customer_conversion_goals, [
        {"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": False},
        {"category": "PAGE_VIEW", "origin": "WEBSITE", "biddable": True},
        {"category": "DEFAULT", "origin": "WEBSITE", "biddable": False},
    ], dry_run=False)
    check(res["updated_goals"] == 3 and not client.log[-1][2].validate_only, f"application reelle : {res}")
    check([op.update.biddable for op in client.log[-1][2].operations] == [False, True, False], "valeurs envoyees")
    # Campagne App independante : couper DOWNLOAD/APP au compte est permis.
    kill_app = [{"category": "DOWNLOAD", "origin": "APP", "biddable": False}]
    res = call(main.set_customer_conversion_goals, kill_app, dry_run=False)
    check(res["updated_goals"] == 1 and res["warnings"] == [], f"App independante non bloquee : {res}")
    # Campagne App dependante (aucune action choisie) : signalee, heritiere,
    # refusee en reel sans allow_app_goal_change.
    APP_STATE["campaign"] = dict(APP_CAMPAIGN, selective_optimization={})
    try:
        QUERY_LOG.clear()
        res = call(main.set_customer_conversion_goals, kill_app)
        check(not last_queries("FROM conversion_action"), "aucune action choisie : pas de requete conversion_action")
        check(res["valid"] is True and len(res["warnings"]) == 1 and APP_ID in res["warnings"][0], f"alerte App dependante : {res['warnings']}")
        check(APP_ID in [c["id"] for c in res["campaigns_following_account_goals"]], "App dependante listee parmi les heritieres")
        check(any("depend des objectifs du compte" in n for n in res["notices"]), "dependance dans notices")
        client.log.clear()
        res = call(main.set_customer_conversion_goals, kill_app, dry_run=False)
        check("refuse" in res["error"]["message"] and not client.log, f"refus App dependante : {res}")
        res = call(main.set_customer_conversion_goals, kill_app, dry_run=False, allow_app_goal_change=True)
        check(res["updated_goals"] == 1 and not client.log[-1][2].validate_only, f"App force : {res}")
    finally:
        APP_STATE["campaign"] = APP_CAMPAIGN
    # Campagne App au niveau CUSTOMER (ligne de config) malgre ses actions choisies.
    APP_STATE["config_level"] = "CUSTOMER"
    try:
        res = call(main.set_customer_conversion_goals, kill_app)
        check(len(res["warnings"]) == 1 and APP_ID in res["warnings"][0], f"App CUSTOMER : {res['warnings']}")
        ids = [c["id"] for c in res["campaigns_following_account_goals"]]
        check(ids.count(APP_ID) == 1 and APP_ID not in [c["id"] for c in res["campaigns_with_own_goals"]], f"App CUSTOMER listee une fois : {ids}")
        check(not any("aucune campagne App" in n and "DOWNLOAD/APP" in n for n in res["notices"]), "App pas comptee comme heritiere non-App")
        client.log.clear()
        res = call(main.set_customer_conversion_goals, kill_app, dry_run=False)
        check("refuse" in res["error"]["message"] and not client.log, f"refus App CUSTOMER : {res}")
    finally:
        APP_STATE["config_level"] = None
    res = call(main.set_customer_conversion_goals, [{"category": "PURCHASE", "origin": "APP", "biddable": True}])
    check("n'existe pas" in res["error"], f"objectif inconnu : {res}")
    all_off = [dict(g, biddable=False) for g in ACCOUNT_GOALS]
    res = call(main.set_customer_conversion_goals, all_off)
    check(any("Aucun objectif du compte" in w for w in res["warnings"]), "alerte : plus rien de biddable")
    # Cette alerte n'est pas une alerte App : elle ne bloque pas le changement reel.
    client.log.clear()
    res = call(main.set_customer_conversion_goals, all_off, dry_run=False)
    check("error" not in res and res["updated_goals"] == len(res["changes"]) and not client.log[-1][2].validate_only, f"all_off reel non bloque : {res}")
    # Refus de Google : l'apercu est garde et chaque objectif fautif est nomme.
    MUTATE_FAILURES.append(make_google_ads_exception(default_client, 1, 4))
    res = call(main.set_customer_conversion_goals, PLAN_GOALS)
    err = res["error"]
    check(err["after_biddable"] == ["PURCHASE/WEBSITE"] and err["request_id"] == "req-test", f"apercu garde : {err}")
    goal_at = lambda i: f"{err['changes'][i]['category']}/{err['changes'][i]['origin']}"
    check([r["goal"] for r in err["rejected"]] == [goal_at(1), goal_at(4)], f"objectifs fautifs : {err['rejected']}")
    check(err["rejected"][0]["message"] == "Objectif 1 non modifiable." and "IMMUTABLE_FIELD" in err["rejected"][0]["code"], f"raison et code : {err['rejected'][0]}")
    check(not MUTATE_FAILURES, "erreur consommee")

    # --- suggest_brands
    SUGGESTED_BRANDS[:] = [
        {"id": "b-1", "name": "Marque test", "urls": ["marque.example"], "state": "ENABLED"},
        {"id": "b-old", "name": "Marque test", "urls": [], "state": "APPROVED"},
        {"id": "b-own", "name": "Marque test", "urls": [], "state": "UNVERIFIED"},
        {"id": "b-dep", "name": "Marque test", "urls": [], "state": "DEPRECATED"},
        {"id": "b-can", "name": "Marque test", "urls": [], "state": "CANCELLED"},
        {"id": "b-rej", "name": "Marque test", "urls": [], "state": "REJECTED"},
    ]
    client.log.clear()
    res = call(main.suggest_brands, "marque", selected_brand_ids=[" x-1 ", " ", ""])
    check([b["usable"] for b in res["brands"]] == [True, False, True, False, False, False], f"usable par etat : {res['brands']}")
    check(res["brands"][0] == {"id": "b-1", "name": "Marque test", "urls": ["marque.example"], "state": "ENABLED", "usable": True}, f"suggest_brands : {res}")
    check(res["brands"][1]["usable"] is False and "APPROVED = ancien id" in res["note"], "APPROVED non utilisable")
    sv, m, request = client.log[-1]
    check((sv, m) == ("BrandSuggestionService", "suggest_brands") and request.brand_prefix == "marque", "requete SuggestBrands")
    check(list(request.selected_brands) == ["x-1"] and request.customer_id == CID, "marques deja choisies + compte")
    SUGGESTED_BRANDS.clear()
    res = call(main.suggest_brands, "zzz inconnue")
    check(res["brand_count"] == 0 and "Plan B" in res["note"], f"aucune marque : {res}")
    check("prefix" in call(main.suggest_brands, "  ")["error"], "prefix vide refuse")

    # --- create_brand_list
    client.log.clear()
    QUERY_LOG.clear()
    res = call(main.create_brand_list, "Marque", ["b-1"])
    check(res.get("dry_run") is True and res["operations"] == {"shared_set": 1, "shared_criterion": 1}, f"liste dry run : {res}")
    sv, m, request = client.log[-1]
    check((sv, m) == ("GoogleAdsService", "mutate") and request.validate_only, "requete atomique validate_only")
    check(all("shared_set.status = 'ENABLED'" in q for q in last_queries("shared_set.name")), "doublon : listes actives seulement")
    res = call(main.create_brand_list, "Marque", ["b-1"], dry_run=False)
    check(res["shared_set_id"] == "101" and res["brands_added"] == 1, f"liste creee : {res}")
    # Nom avec apostrophe : echappe dans la requete GAQL, doublon detecte.
    BRAND_STATE["existing_names"] = ["Marque", "L'Atelier"]
    try:
        client.log.clear()
        res = call(main.create_brand_list, "Marque", ["b-1"])
        check(res["error"]["shared_set_id"] == "555" and not client.log, f"nom deja pris : {res}")
        res = call(main.create_brand_list, "L'Atelier", ["b-1"])
        check(res["error"]["shared_set_id"] == "555" and not client.log, f"apostrophe echappee : {res}")
        check(any("shared_set.name = 'L\\'Atelier'" in q for q in QUERY_LOG), "echappement GAQL")
    finally:
        BRAND_STATE["existing_names"] = []
    check("brand_entity_ids" in call(main.create_brand_list, "X", [])["error"], "liste vide refusee")
    # Nom finissant par un antislash : double dans la requete GAQL.
    QUERY_LOG.clear()
    res = call(main.create_brand_list, "Marque \\", ["b-1"])
    check(res.get("dry_run") is True, f"antislash accepte : {res}")
    check(any("shared_set.name = 'Marque \\\\'" in q for q in QUERY_LOG), f"antislash echappe : {QUERY_LOG[-1:]}")
    # Limite de longueur : 255 accepte, 256 refuse sans appel.
    check(call(main.create_brand_list, "x" * 255, ["b-1"]).get("dry_run") is True, "255 caracteres acceptes")
    client.log.clear()
    check("255" in call(main.create_brand_list, "x" * 256, ["b-1"])["error"] and not client.log, "256 caracteres refuses")

    # --- set_pmax_brand_exclusion
    client.log.clear()
    res = call(main.set_pmax_brand_exclusion, PMAX_ID, "555")
    check(res.get("dry_run") is True and res["brand_lists_before"] == [], f"exclusion dry run : {res}")
    check(res["brand_lists_after"][0]["shared_set"] == BRAND_SET_RN and res["brand_list"]["brands"][0]["display_name"] == "Marque test", "avant / apres")
    sv, m, request = client.log[-1]
    check((sv, m) == ("CampaignCriterionService", "mutate_campaign_criteria") and request.validate_only, "requete exclusion")
    crit = request.operations[0].create
    check(crit.negative and crit.brand_list.shared_set == BRAND_SET_RN, "critere negatif BrandListInfo")
    check(any("campaign_criterion.status != 'REMOVED'" in q for q in last_queries("BRAND_LIST")), "criteres supprimes exclus")
    res = call(main.set_pmax_brand_exclusion, PMAX_ID, BRAND_SET_RN, dry_run=False)
    check(res["added"] and not client.log[-1][2].validate_only, f"exclusion reelle : {res}")
    check(res["brand_lists_after"][-1]["criterion_id"] == "700", f"criterion_id extrait : {res['brand_lists_after']}")
    BRAND_STATE["attached"] = [(BRAND_SET_RN, True)]
    try:
        client.log.clear()
        res = call(main.set_pmax_brand_exclusion, PMAX_ID, "555", dry_run=False)
        check(res["already_attached"] is True and not client.log, f"deja liee : {res}")
        BRAND_STATE["attached"] = [(BRAND_SET_RN, False)]  # ciblage positif : pas une exclusion
        res = call(main.set_pmax_brand_exclusion, PMAX_ID, "555")
        check("already_attached" not in res and res["valid"] is True, f"liste positive != exclusion : {res}")
        other_rn = f"customers/{CID}/sharedSets/999"
        BRAND_STATE["attached"] = [(other_rn, True)]  # une autre liste deja exclue
        client.log.clear()
        res = call(main.set_pmax_brand_exclusion, PMAX_ID, "555", dry_run=False)
        check("already_attached" not in res and res.get("added") and len(client.log) == 1, f"autre liste exclue : {res}")
        check([b["shared_set"] for b in res["brand_lists_after"]] == [other_rn, BRAND_SET_RN], "avant + nouvelle liste")
    finally:
        BRAND_STATE["attached"] = []
    res = call(main.set_pmax_brand_exclusion, SEARCH_ID, "555")
    check("Performance Max" in res["error"]["message"], f"campagne non PMax refusee : {res}")
    client.log.clear()
    res = call(main.set_pmax_brand_exclusion, "1000000099", "555")
    check("introuvable" in res["error"] and not client.log, f"campagne inconnue : {res}")
    res = call(main.set_pmax_brand_exclusion, PMAX_ID, "556")
    check("BRANDS" in res["error"]["message"], f"liste non BRANDS refusee : {res}")
    client.log.clear()
    res = call(main.set_pmax_brand_exclusion, PMAX_ID, "557")
    check("active" in res["error"]["message"] and not client.log, f"liste supprimee refusee : {res}")
    res = call(main.set_pmax_brand_exclusion, PMAX_ID, "999")
    check("introuvable" in res["error"], f"liste absente : {res}")
    check("shared_set invalide" in call(main.set_pmax_brand_exclusion, PMAX_ID, "abc")["error"], "shared_set invalide")


def test_tools_registered() -> None:
    tools = asyncio.run(main.mcp.list_all_tools())
    by_name = {t.name: t for t in tools}
    expected = [
        "list_asset_groups", "get_campaign_conversion_goals", "set_campaign_target_roas",
        "set_campaign_conversion_goals", "add_search_themes", "set_campaign_url_expansion",
        "create_search_campaign", "create_pmax_campaign", "add_search_ad_group",
        "add_campaign_assets",
        "list_user_lists", "get_customer_match_status", "create_customer_match_list",
        "upload_customer_match_members",
        "set_customer_conversion_goals", "suggest_brands", "create_brand_list",
        "set_pmax_brand_exclusion",
    ]
    for name in expected:
        check(name in by_name, f"outil non enregistre : {name}")
        check(bool(by_name[name].description), f"description vide : {name}")
        if name.startswith(("set_", "add_", "create_", "upload_")):
            check("WRITE TOOL" in by_name[name].description, f"mention WRITE TOOL : {name}")
            check("dry_run" in by_name[name].inputSchema["properties"], f"dry_run : {name}")
        else:
            check("WRITE TOOL" not in by_name[name].description, f"outil de lecture : {name}")
    check(by_name["create_search_campaign"].inputSchema["properties"]["spec"]["type"] == "object", "spec: object")
    members_schema = by_name["upload_customer_match_members"].inputSchema["properties"]["members"]
    check(members_schema["type"] == "array" and members_schema["items"]["type"] == "object", f"members: array d'objets ({members_schema})")
    check("remove" in by_name["upload_customer_match_members"].inputSchema["properties"], "remove")
    status_props = by_name["get_customer_match_status"].inputSchema["properties"]
    check("user_list_id" in status_props and "job_resource_name" in status_props, "parametres du statut")
    check(by_name["create_pmax_campaign"].inputSchema["properties"]["spec"]["type"] == "object", "spec PMax: object")
    check("clone_from_campaign_id" in by_name["create_pmax_campaign"].description, "clonage documente")
    check(len(tools) == 9 + 10 + 4 + 4, f"{len(tools)} outils (9 + 10 + 4 Customer Match + 4 objectifs/marques attendus)")
    check(main.READ_TOOL_NAMES | main.WRITE_TOOL_NAMES == set(by_name), "chaque outil est classe")
    check(len(main.READ_TOOL_NAMES) == 11 and len(main.WRITE_TOOL_NAMES) == 16, "11 lectures, 16 ecritures")
    for name in ("set_customer_conversion_goals", "create_brand_list", "set_pmax_brand_exclusion"):
        check(by_name[name].inputSchema["properties"]["dry_run"]["default"] is True, f"dry_run=true par defaut : {name}")
    # Journal : dry_run effectif (defaut de l'outil quand l'argument manque).
    check(main.mcp.effective_dry_run("create_brand_list", {}) is True, "defaut true journalise en dry run")
    check(main.mcp.effective_dry_run("create_brand_list", {"dry_run": False}) is False, "dry_run=false explicite")
    check(main.mcp.effective_dry_run("set_campaign_status", {}) is False, "defaut false des anciens outils")
    check(main.mcp.effective_dry_run("set_campaign_status", {"dry_run": True}) is True, "dry_run=true explicite")
    for text, expected in (("false", False), ("0", False), ("true", True), ("1", True)):
        check(main.mcp.effective_dry_run("create_brand_list", {"dry_run": text}) is expected, f"dry_run texte {text!r}")
    check(main.mcp.effective_dry_run("outil_inconnu", {}) is False, "outil inconnu")
    # Ligne de journal reellement emise par call_tool (outil non execute).
    logged, saved = [], (main.log, main.can_write, main.FastMCP.call_tool)

    async def _no_exec(self, name, arguments):
        return None

    main.log, main.can_write, main.FastMCP.call_tool = logged.append, (lambda user: True), _no_exec
    try:
        for name, args, dry in (
            ("create_brand_list", {}, True),
            ("create_brand_list", {"dry_run": "false"}, False),
            ("set_campaign_status", {}, False),
            ("set_campaign_status", {"dry_run": True}, True),
            ("get_campaigns", {}, False),
        ):
            logged.clear()
            asyncio.run(main.mcp.call_tool(name, args))
            check(len(logged) == 1 and ("(dry run)" in logged[0]) is dry, f"journal {name} {args} : {logged}")
    finally:
        main.log, main.can_write, main.FastMCP.call_tool = saved
    # Instructions : chaque outil d'ecriture y est nomme ; defauts dry_run explicites.
    missing = sorted(n for n in main.WRITE_TOOL_NAMES if n not in main.mcp.instructions)
    check(not missing, f"outils d'ecriture absents des instructions : {missing}")
    m = re.search(r"Write tools \(([^)]*)\)", main.mcp.instructions)
    listed = {n.strip() for n in m.group(1).split(",")} if m else set()
    check(listed == main.WRITE_TOOL_NAMES, f"liste 'Write tools (...)' : {sorted(listed ^ main.WRITE_TOOL_NAMES)}")
    check("set_customer_conversion_goals, create_brand_list and set_pmax_brand_exclusion default to dry_run=true" in main.mcp.instructions, "trois outils dry_run=true par defaut")
    check("(including set_campaign_conversion_goals and add_negative_keywords) applies the change immediately" in main.mcp.instructions, "autres outils : application immediate")
    goals_schema = by_name["set_customer_conversion_goals"].inputSchema["properties"]["goals"]
    check(goals_schema["type"] == "array" and goals_schema["items"]["type"] == "object", f"goals: array d'objets ({goals_schema})")
    for name, tool in by_name.items():
        is_write = name in main.WRITE_TOOL_NAMES
        check(is_write == name.startswith(("set_", "add_", "create_", "upload_")), f"classement : {name}")
        check(tool.annotations is not None and tool.annotations.readOnlyHint is (not is_write), f"readOnlyHint : {name}")
        if name != "list_accounts":
            check("customer_id" in tool.inputSchema["properties"], f"customer_id expose : {name}")
            check("customer_id" not in tool.inputSchema.get("required", []), f"customer_id facultatif : {name}")


# ---------------------------------------------------------------------------
# 3) Cles d'acces et comptes servis (conventions LBC)
# ---------------------------------------------------------------------------


def test_auth_keys() -> None:
    keys, warnings = main.parse_auth_keys(
        "cle-complete-0123456789:thierry:full, cle-lecture-0123456789:agence:read,"
        " sans-role-0123456789abc:marie, court:x:full, cle/avec/barre-0123456789:y:full,"
        " role-inconnu-0123456789:z:admin, lecture-fr-0123456789:w:Lecture,,"
    )
    check(keys == {
        "cle-complete-0123456789": {"name": "thierry", "role": "full"},
        "cle-lecture-0123456789": {"name": "agence", "role": "read"},
        "sans-role-0123456789abc": {"name": "marie", "role": "full"},
        "lecture-fr-0123456789": {"name": "w", "role": "read"},
    }, f"cles : {keys}")
    check(len(warnings) == 3, f"3 cles ignorees : {warnings}")
    check("n. 4 (x)" in warnings[0] and "16 caracteres" in warnings[0], f"secret court : {warnings[0]}")
    check("n. 5 (y)" in warnings[1] and "interdits" in warnings[1], f"caractere interdit : {warnings[1]}")
    check("n. 6 (z)" in warnings[2] and "'admin'" in warnings[2], f"role inconnu : {warnings[2]}")
    check(main.parse_auth_keys("") == ({}, []) and main.parse_auth_keys(" , ") == ({}, []), "AUTH_KEYS vide")
    check(not any(k in " ".join(warnings) for k in ("cle-complete", "role-inconnu-0123")), "aucun secret dans les avertissements")
    # find_key : cle chargee depuis l'environnement de ce test.
    check(main.find_key("offline-test-secret-0123456789") == {"name": "tests", "role": "full"}, "find_key")
    check(main.find_key("offline-test-secret-012345678") is None, "prefixe refuse")
    check(main.find_key("") is None and main.find_key("inconnue-0123456789abcdef") is None, "cle inconnue")


def test_customer_ids() -> None:
    check(main.SERVED_CUSTOMER_IDS == [CID] and not main.CUSTOMER_ID_ERRORS, f"compte servi : {main.SERVED_CUSTOMER_IDS}")
    ids, errors = main.parse_customer_ids(" 123-456-7890 , 1112223333;111-222-3333,, 42 ")
    check(ids == [CID, "1112223333"], f"ids normalises, doublon retire : {ids}")
    check(len(errors) == 1 and "'42'" in errors[0], f"erreur sur 42 : {errors}")
    check(main.parse_customer_ids("") == ([], []), "vide = aucun compte")

    check(main.resolve_cid(None) == CID and main.resolve_cid("") == CID and main.resolve_cid("  ") == CID, "compte par defaut")
    check(main.resolve_cid("123-456-7890") == CID, "compte explicite normalise")
    for bad, fragment in (("111-222-3333", "n'est pas servi"), ("12345", "10 chiffres")):
        try:
            main.resolve_cid(bad)
        except ValueError as ex:
            check(fragment in str(ex), f"message pour {bad} : {ex}")
        else:
            check(False, f"{bad} aurait du etre refuse")

    saved = list(main.SERVED_CUSTOMER_IDS)
    main.SERVED_CUSTOMER_IDS.clear()  # serveur sans GOOGLE_ADS_CUSTOMER_ID
    try:
        try:
            main.resolve_cid(None)
        except ValueError as ex:
            check("customer_id requis" in str(ex) and "list_accounts" in str(ex), f"sans defaut : {ex}")
        else:
            check(False, "customer_id aurait du etre requis")
        check(main.resolve_cid("111-222-3333") == "1112223333", "sans restriction : tout compte")
    finally:
        main.SERVED_CUSTOMER_IDS[:] = saved


def test_list_accounts_scope() -> None:
    other, mcc = "1112223333", "9998887777"
    accessible = SimpleNamespace(resource_names=[f"customers/{c}" for c in (CID, other, mcc)])
    fake_client = SimpleNamespace(
        get_service=lambda name: SimpleNamespace(list_accessible_customers=lambda: accessible)
    )
    queried: list[str] = []

    def fake_query(customer_id: str, query: str, limit: int = 200) -> list[dict]:
        queried.append(customer_id)
        if "FROM customer_client" in query:
            return [
                {"customer_client": {"id": c, "descriptive_name": n, "level": lvl}}
                for c, n, lvl in ((mcc, "Gestionnaire", 0), (CID, "Marque test", 1), (other, "Autre marque", 1))
            ]
        return [{"customer": {"descriptive_name": f"Compte {customer_id}", "currency_code": "CAD"}}]

    saved = (main.get_client, main.run_query)
    main.get_client, main.run_query = (lambda: fake_client), fake_query
    os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = mcc
    try:
        res = json.loads(main.list_accounts())
    finally:
        main.get_client, main.run_query = saved
        os.environ.pop("GOOGLE_ADS_LOGIN_CUSTOMER_ID", None)
    check([a["customer_id"] for a in res["directly_accessible"]] == [CID, mcc], f"comptes directs : {res}")
    check([a["customer_id"] for a in res["under_manager"]] == [mcc, CID], f"sous le MCC : {res}")
    check(res["default_customer_id"] == CID and res["served_customer_ids"] == [CID], "compte par defaut indique")
    check(res["other_accounts_hidden"] == 1 and "masques" in res["note"], f"autre compte masque : {res}")
    check(other not in json.dumps(res) and "Autre marque" not in json.dumps(res), "rien de l'autre compte")
    check(other not in queried, "aucune requete sur l'autre compte")


def main_() -> None:
    versions = ["v22", None]
    for version in versions:
        client = make_client(version)
        label = version or f"defaut ({_DEFAULT_VERSION})"
        for test in (
            test_search_campaign_ops, test_pmax_campaign_ops, test_ad_group_and_asset_ops,
            test_search_theme_ops, test_target_roas_ops, test_conversion_goal_ops,
            test_url_expansion_ops, test_customer_match_ops, test_customer_goal_ops,
            test_brand_ops,
        ):
            test(client)
            print(f"OK  {test.__name__:32s} API {label}")
    test_validation_errors()
    print(f"OK  {test_validation_errors.__name__:32s}")
    test_pmax_validation_errors()
    print(f"OK  {test_pmax_validation_errors.__name__:32s}")
    test_customer_match_normalization()
    print(f"OK  {test_customer_match_normalization.__name__:32s}")
    test_tools_registered()
    print(f"OK  {test_tools_registered.__name__:32s}")
    for test in (test_auth_keys, test_customer_ids, test_list_accounts_scope):
        test()
        print(f"OK  {test.__name__:32s}")
    test_tools_end_to_end(make_client(None))
    print(f"OK  {test_tools_end_to_end.__name__:32s} API defaut ({_DEFAULT_VERSION})")
    test_goal_and_brand_tools_end_to_end(make_client(None))
    print(f"OK  {test_goal_and_brand_tools_end_to_end.__name__:32s} API defaut ({_DEFAULT_VERSION})")
    print(f"\n{CHECKS} verifications reussies, aucune requete reseau.")


if __name__ == "__main__":
    main_()
