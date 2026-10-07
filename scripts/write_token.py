import os, pathlib, sys
value=os.environ.get('YOUTUBE_TOKEN_JSON','').strip()
if not value:
    print('ERROR: GitHub Secret YOUTUBE_TOKEN_JSON is missing.')
    sys.exit(2)
p=pathlib.Path('config/youtube_token.json')
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(value, encoding='utf-8')
print('OAuth token file prepared from GitHub Secret.')
