from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
SCOPES=['https://www.googleapis.com/auth/youtube.upload']
root=Path(__file__).resolve().parent
client=root/'config'/'client_secret.json'
out=root/'config'/'youtube_token.json'
if not client.exists():
    raise SystemExit(f'Missing: {client}\nPut your NEW client_secret.json there. Never upload it to GitHub.')
flow=InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
creds=flow.run_local_server(port=0, prompt='consent', access_type='offline')
out.write_text(creds.to_json(), encoding='utf-8')
print('\nSUCCESS')
print(f'Created: {out}')
print('Do NOT upload this file to a public repository. Put its FULL contents in GitHub Secret YOUTUBE_TOKEN_JSON.')
