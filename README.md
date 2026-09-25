# Serveur MCP Google Ads — Les Belles Combines

Ce serveur expose le compte Google Ads des Belles Combines à Claude via un serveur MCP (Model Context Protocol) hébergé sur Railway. Une fois connecté, Claude peut analyser les campagnes, les mots-clés et les termes de recherche. Si l'écriture est activée et la clé d'accès le permet, il peut aussi :

- mettre des campagnes en pause, ajuster des budgets et ajouter des mots-clés négatifs ;
- régler les objectifs de conversion et le ROAS cible ;
- enrichir une campagne Performance Max (thèmes de recherche, expansion d'URL) ;
- créer des campagnes Search ou Performance Max complètes ;
- bâtir des audiences Customer Match.

```
Claude (claude.ai) ──HTTPS──► Railway (ce serveur) ──API──► Google Ads (compte de la marque)
```

## D'où vient ce serveur

- **Base** : le serveur Google Ads de Snoc ([`Snoc-Studio/snoc-google-ads-mcp`](https://github.com/Snoc-Studio/snoc-google-ads-mcp)). Il apporte les mêmes 23 outils, les mêmes validations avant l'appel à Google et les mêmes tests hors ligne.
- **Conventions LBC** : elles viennent du serveur Meta Ads des Belles Combines (`TheTitoiso/lbc-meta-ads-cloud`). Chaque personne a sa propre clé d'accès avec un rôle, la marque et le compte sont décrits par les variables d'environnement, rien n'est en dur dans le code, et `/health` indique la marque et la version.

| | Serveur Snoc | Ce serveur (LBC) |
|---|---|---|
| Accès | un seul `MCP_SECRET` | `AUTH_KEYS` : une clé par personne, rôle `full` ou `read` (même format que le serveur Meta Ads) |
| Écritures | `GOOGLE_ADS_ALLOW_WRITES` | `GOOGLE_ADS_ALLOW_WRITES` **et** une clé `full` ; les outils d'écriture sont masqués aux clés `read` |
| Compte visé | `customer_id` obligatoire dans chaque outil | `GOOGLE_ADS_CUSTOMER_ID` sert de compte par défaut (`customer_id` devient facultatif) ; tout autre compte est refusé |
| `list_accounts` | tous les comptes accessibles | seulement le ou les comptes servis, plus le MCC ; les autres sont masqués |
| Permissions dans claude.ai | — | annotations MCP `readOnlyHint` sur chaque outil (lecture ou écriture) |
| Journaux Railway | — | chaque appel d'outil est journalisé avec le nom de la clé (jamais les données) |

Limiter le serveur au compte de la marque évite qu'une conversation touche par erreur un autre compte auquel le même utilisateur Google a accès (par exemple celui de Snoc).

---

## Variables d'environnement

| Variable | Obligatoire | Description |
|---|---|---|
| `AUTH_KEYS` | oui | `secret:nom:role[,secret2:nom2:role2]`. Le secret fait au moins 16 caractères (lettres, chiffres, `-` `_` `.` `~`) ; le rôle est `full` (lecture + écriture) ou `read` (lecture seule), `full` par défaut. Pour générer un secret : `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Une clé invalide est ignorée, avec un avertissement dans les journaux. |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | oui | Token du Centre API du compte administrateur (MCC). |
| `GOOGLE_ADS_CLIENT_ID` / `GOOGLE_ADS_CLIENT_SECRET` | oui | Client OAuth « Application de bureau » (Google Cloud Console). |
| `GOOGLE_ADS_REFRESH_TOKEN` | oui | Refresh token OAuth de l'utilisateur Google qui a accès au compte. |
| `GOOGLE_ADS_CUSTOMER_ID` | recommandé | Compte Google Ads servi (10 chiffres, tirets acceptés). C'est le compte par défaut des outils, et tout autre compte est refusé. Pour plusieurs comptes, séparer les ID par des virgules : le premier est le compte par défaut. Laissé vide, le serveur accepte tout compte accessible et `customer_id` devient obligatoire. |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | si MCC | ID du compte administrateur (MCC), sans tirets, quand l'accès au compte passe par le MCC. |
| `GOOGLE_ADS_ALLOW_WRITES` | non | `true` autorise les modifications (clés `full` seulement). Par défaut ou avec `false`, le serveur est en lecture seule pour tout le monde. |
| `BRAND_NAME` | non | Nom de la marque, repris dans les instructions du serveur, `/health` et les journaux. |
| `MCP_ALLOWED_HOSTS` | non | Domaines autorisés dans l'en-tête `Host`. Vide par défaut : la protection anti-DNS-rebinding du SDK est désactivée, puisque l'accès est déjà protégé par `AUTH_KEYS` (voir Dépannage, 421). |
| `PORT` | non | Fourni par Railway (8080 par défaut). |

Le serveur **refuse de démarrer** sans clé valide dans `AUTH_KEYS`, ou si `GOOGLE_ADS_CUSTOMER_ID` est invalide. Il démarre en revanche sans identifiants Google : `/health` indique alors `"google_ads_configured": false` et les outils renvoient une erreur claire.

## Reprendre les identifiants du serveur local

Si un serveur Google Ads fonctionne déjà en local (Claude Desktop ou Cowork), ses identifiants se reportent tels quels dans les variables Railway :

| Variable Railway | `.env` local | `google-ads.yaml` | `application_default_credentials.json` (gcloud) |
|---|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | `GOOGLE_ADS_DEVELOPER_TOKEN` | `developer_token` | — |
| `GOOGLE_ADS_CLIENT_ID` | `GOOGLE_ADS_CLIENT_ID` | `client_id` | `client_id` |
| `GOOGLE_ADS_CLIENT_SECRET` | `GOOGLE_ADS_CLIENT_SECRET` | `client_secret` | `client_secret` |
| `GOOGLE_ADS_REFRESH_TOKEN` | `GOOGLE_ADS_REFRESH_TOKEN` | `refresh_token` | `refresh_token` |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | `login_customer_id` | — |
| `GOOGLE_ADS_CUSTOMER_ID` | `GOOGLE_ADS_CUSTOMER_ID` | — | — |

Le fichier gcloud se trouve dans `~/.config/gcloud/` sur Mac. Si le serveur local passe par un compte de service (fichier JSON avec `private_key`), ce fichier n'est pas utilisable ici : il faut générer un refresh token OAuth (étape 3 ci-dessous).

⚠️ **Ce dépôt est public** : ces valeurs ne vont que dans les variables Railway, jamais dans un fichier du dépôt. Le `.gitignore` exclut `.env`, `google-ads.yaml` et les fichiers `credentials*.json` et `token*.json`.

## Obtenir de nouveaux identifiants (si besoin)

1. **Developer token** : dans le compte administrateur (MCC), menu **Admin → Configuration → Centre API**. Le niveau « Accès Explorer », obtenu immédiatement, suffit : il fonctionne sur les comptes réels jusqu'à 2 880 opérations par jour. Le niveau « Accès Basic » (15 000 opérations par jour) est à demander dans le même écran.
2. **Client OAuth** : sur https://console.cloud.google.com/, dans un projet où la **Google Ads API** est activée :
   - Écran de consentement OAuth : type **Externe**, puis ⚠️ **passer l'application « En production »**. En mode « Test », le refresh token expire tous les 7 jours.
   - **Identifiants → Créer des identifiants → ID client OAuth → Application de bureau** : noter le Client ID et le Client Secret.
3. **Refresh token**, sur ton ordinateur (il faut un navigateur) :
   ```bash
   pip install google-auth-oauthlib
   python get_refresh_token.py
   ```
   Se connecter avec le compte Google qui a accès au compte Google Ads de la marque (directement ou via le MCC).

## Déploiement sur Railway

1. Le service est créé dans le projet Railway des Belles Combines, à côté du serveur Meta Ads, et déployé depuis ce dépôt : chaque `git push` sur la branche suivie redéploie automatiquement. La commande de démarrage (`python main.py`) et le healthcheck (`/health`) sont définis dans `railway.json`.
2. Les variables se règlent dans l'onglet **Variables** du service (voir le tableau plus haut). Après une modification, cliquer **Deploy** pour appliquer les changements en attente.
3. Le domaine public se crée dans **Settings → Networking → Generate Domain** et donne une URL du type `https://xxxx.up.railway.app`.
4. Pour vérifier, ouvrir `https://<domaine>/health` :
   ```json
   {"status": "ok", "service": "lbc-google-ads-mcp", "brand": "Les Belles Combines", "version": "1.0.0",
    "writes_enabled": true, "google_ads_configured": true, "default_customer_configured": true,
    "auth_keys": {"full": 1, "read": 0}, "tools": {"read": ["…"], "write": ["…"]}, "uptime_s": 42}
   ```

## Connecter à Claude

1. Sur https://claude.ai : **Paramètres → Connecteurs → Ajouter un connecteur personnalisé**.
2. Saisir l'URL `https://<domaine>/mcp/<secret>`, où `<secret>` est la partie avant le premier `:` d'une clé de `AUTH_KEYS`. Laisser vides les champs OAuth avancés.
3. Activer le connecteur dans une conversation (menu des outils). Grâce aux annotations `readOnlyHint`, les outils de lecture peuvent être autorisés en bloc tandis que les outils d'écriture restent soumis à confirmation.

Les clients qui gèrent les en-têtes (Claude Code, API) peuvent aussi utiliser `https://<domaine>/mcp` avec l'en-tête `Authorization: Bearer <secret>`.

**Une clé par personne** : par exemple `secret1:thierry:full,secret2:agence:read`. Une clé `read` ne voit que les 10 outils de lecture. Pour révoquer une clé, il suffit de la retirer de `AUTH_KEYS` et de redéployer.

## Outils exposés à Claude

`customer_id` est **facultatif** dans tous les outils : sans lui, le serveur utilise le compte par défaut (`GOOGLE_ADS_CUSTOMER_ID`).

**Lecture** (toutes les clés)

| Outil | Rôle |
|---|---|
| `list_accounts` | Comptes servis, avec le compte par défaut (et les comptes sous le MCC si configuré) |
| `run_gaql` | Requête GAQL libre, qui couvre tout le reste (annonces, audiences, historique des modifications, recommandations…) |
| `get_campaigns` | Campagnes avec statut, type et budget quotidien |
| `get_campaign_performance` | Impressions, clics, CTR, CPC, coût et conversions par campagne |
| `get_keyword_performance` | Top mots-clés par coût, avec quality score |
| `get_search_terms` | Termes de recherche réels (chasse au gaspillage) |
| `list_asset_groups` | Groupes d'assets d'une campagne Performance Max, avec thèmes de recherche et signaux d'audience |
| `get_campaign_conversion_goals` | Objectifs de conversion d'une campagne comparés à ceux du compte |
| `list_user_lists` | Listes d'audience (Customer Match, remarketing…) : tailles, éligibilité, taux de correspondance |
| `get_customer_match_status` | État d'une liste Customer Match et/ou d'un job d'import |

**Écriture** : clé `full` et `GOOGLE_ADS_ALLOW_WRITES=true` requis. Chaque outil accepte `dry_run=true`, qui valide sans rien appliquer.

| Outil | Rôle |
|---|---|
| `set_campaign_status` | Mettre en pause ou réactiver une campagne (jamais de suppression) |
| `set_campaign_budget` | Modifier un budget quotidien (refuse les budgets partagés sauf accord explicite) |
| `add_negative_keywords` | Ajouter des mots-clés négatifs à une campagne |
| `set_campaign_target_roas` | Fixer ou retirer le ROAS cible (ratio : 3.0 = 300 %) |
| `set_campaign_conversion_goals` | Objectifs de conversion propres à la campagne, catégories « biddable » (ex. `PURCHASE` seulement) |
| `add_search_themes` | Ajouter des thèmes de recherche à un groupe d'assets Performance Max (25 au maximum, doublons ignorés) |
| `set_campaign_url_expansion` | Activer ou désactiver l'expansion d'URL finale (Performance Max) |
| `create_search_campaign` | Campagne Search complète en une requête atomique (budget, ciblage, groupes d'annonces, annonces responsives, mots-clés, sitelinks, callouts), créée en pause par défaut |
| `create_pmax_campaign` | Campagne Performance Max complète (flux Merchant Center, assets existants, filtre de fiches, signaux) ; peut cloner une campagne existante |
| `add_search_ad_group` | Ajouter un groupe d'annonces à une campagne Search existante |
| `add_campaign_assets` | Créer des sitelinks et callouts, puis les lier à une campagne |
| `create_customer_match_list` | Créer une liste Customer Match vide (refuse un nom déjà utilisé) |
| `upload_customer_match_members` | Ajouter ou retirer des membres à partir de coordonnées brutes, normalisées et hachées en SHA-256 sur le serveur |

Les montants `*_micros` de l'API sont des millionièmes de la devise du compte ; les outils renvoient aussi les montants convertis. Pour `create_search_campaign` et `create_pmax_campaign`, demander à Claude de montrer la spécification et de lancer un `dry_run` avant la création réelle. Les limites de Google (longueur des titres et descriptions, nombre d'assets…) sont vérifiées avant l'appel à l'API.

**Customer Match** : les coordonnées transitent par le serveur le temps d'être normalisées et hachées en mémoire. Seules les empreintes (plus le pays et le code postal) partent chez Google ; rien n'est journalisé ni stocké. Le consentement est déclaré `GRANTED` : n'importer que des clients qui ont consenti. Google traite l'import en 6 à 48 h.

---

## Dépannage

| Symptôme | Cause probable et remède |
|---|---|
| `401 unauthorized` en ajoutant le connecteur | Le secret de l'URL n'est pas dans `AUTH_KEYS`, ou la clé a été ignorée : chercher `ATTENTION AUTH_KEYS` dans les journaux Railway. L'URL doit se terminer par `/mcp/<secret>`. |
| Les outils d'écriture n'apparaissent pas dans Claude | La clé est en rôle `read`, ou `GOOGLE_ADS_ALLOW_WRITES` n'est pas `true`. Corriger, redéployer, puis désactiver et réactiver le connecteur. |
| « Le compte X n'est pas servi par ce serveur » | Le compte demandé n'est pas dans `GOOGLE_ADS_CUSTOMER_ID` : l'ajouter (séparé par une virgule) s'il doit l'être. |
| `DEVELOPER_TOKEN_NOT_APPROVED` | Token resté au niveau « Compte test » : vérifier le niveau d'accès dans le Centre API. |
| `invalid_grant` | Refresh token expiré ou révoqué, le plus souvent parce que l'écran de consentement est resté « En test ». Le publier en production, régénérer le token et mettre à jour la variable. |
| `USER_PERMISSION_DENIED` | L'utilisateur OAuth n'a pas accès au compte, ou il y accède via un MCC et `GOOGLE_ADS_LOGIN_CUSTOMER_ID` est absent ou incorrect. |
| `CUSTOMER_NOT_ENABLED` | Compte publicitaire non activé (sans facturation) ou annulé. |
| `421` « Invalid Host header » | `MCP_ALLOWED_HOSTS` ne contient pas exactement le domaine de l'URL du connecteur : le vider ou le corriger. |
| Déploiement Railway en échec | Onglet **Deployments → View logs** : `AUTH_KEYS` absent ou sans clé valide, ou `GOOGLE_ADS_CUSTOMER_ID` invalide. |

## Sécurité

- L'URL complète (avec le secret) **est** la clé du serveur : ne pas la partager ni la publier. Pour la révoquer, retirer ou changer la clé dans `AUTH_KEYS`, puis mettre à jour le connecteur.
- Ce dépôt est public : aucune valeur réelle n'y figure, elles ne vivent que dans les variables Railway. Il est conseillé de le passer en **privé** (GitHub → Settings → General → Danger Zone → Change visibility).
- Les outils d'écriture sont volontairement limités : aucune suppression (campagnes, annonces, mots-clés, listes), refus des budgets partagés sans confirmation, campagnes créées **en pause** par défaut et ciblage géographique obligatoire à la création.

## Tests

Les tests se lancent sans identifiants et sans aucune requête réseau :

```bash
pip install -r requirements.txt
python tests/offline_build_test.py   # outils, opérations Google Ads (API v22 et version par défaut), Customer Match, clés, comptes servis
python tests/http_auth_test.py       # couche HTTP : /health, 401, rôles full / read, Bearer, compte par défaut
```

## Structure du projet

```
llbc-google-ads-mcp/
├── main.py                    # Le serveur MCP (démarré par Railway)
├── tests/
│   ├── offline_build_test.py  # Test hors ligne des outils (base Snoc + conventions LBC)
│   └── http_auth_test.py      # Test hors ligne de la couche HTTP (clés, rôles, /health)
├── get_refresh_token.py       # À exécuter en local, une fois (refresh token OAuth)
├── requirements.txt           # Dépendances épinglées et testées
├── railway.json               # Config Railway (commande de démarrage, healthcheck)
├── .python-version            # Version Python pour le build Railway
├── .env.example               # Liste documentée des variables (sans valeurs)
└── .gitignore
```
