"""
Generation du refresh token Google Ads (a executer SUR TON ORDINATEUR,
pas sur Railway) :

    pip install google-auth-oauthlib
    python get_refresh_token.py

Le script ouvre ton navigateur : connecte-toi avec le compte Google qui a
acces a ton compte Google Ads, accepte, et le refresh token s'affiche dans
le terminal. Ce token va dans la variable GOOGLE_ADS_REFRESH_TOKEN sur Railway.

Prerequis : un client OAuth de type "Application de bureau" (Desktop app)
cree dans Google Cloud Console (voir README, etape 2).
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/adwords"]


def main() -> None:
    client_id = input("Client ID OAuth : ").strip()
    client_secret = input("Client Secret OAuth : ").strip()

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )

    # port=0 : choisit un port local libre automatiquement.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )

    print("\n" + "=" * 60)
    print("TON REFRESH TOKEN (variable GOOGLE_ADS_REFRESH_TOKEN) :\n")
    print(creds.refresh_token)
    print("=" * 60)
    print(
        "\nGarde ce token secret : il donne acces a ton compte Google Ads.\n"
        "Rappel : si ton ecran de consentement OAuth est encore en mode\n"
        "\"Test\", ce token expire au bout de 7 jours - passe l'application\n"
        "\"En production\" (voir README, etape 2)."
    )


if __name__ == "__main__":
    main()
