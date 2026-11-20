#!/usr/bin/env python3
"""Porte publique de la collecte : tunnel Cloudflare dedie et adresse HOST (seulement /api/*).

Usage, depuis la racine du depot (le jeton vient du vault, jamais affiche) :
  ZONE=madera.tools HOST=avis.madera.tools \
  /home/mad/laurianne-sylvestre/scripts/avec-secret.sh <CLE_JETON_CLOUDFLARE> CF python3 -I ops/cloudflare.py

Sans effet si c'est deja fait. Le jeton du connecteur va dans ~/.config/apercu-panneau/cloudflared-token (0600).
Le tunnel partage de mad-server (dev.madera.tools) n'est pas touche.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = 'https://api.cloudflare.com/client/v4'
ZONE = os.environ.get('ZONE', 'madera.tools')
HOST = os.environ.get('HOST', 'avis.madera.tools')
TUNNEL = 'apercu-panneau'
TOKEN_FILE = '/home/mad/.config/apercu-panneau/cloudflared-token'


def api(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 headers={'Authorization': 'Bearer ' + os.environ['CF'].strip(),
                                          'Content-Type': 'application/json'},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)['result']
    except urllib.error.HTTPError as e:
        errors = json.loads(e.read() or b'{}').get('errors')
        raise SystemExit(f'{method} {path.split("?")[0]} : HTTP {e.code} {errors}') from None


def main():
    zones = api('GET', f'/zones?name={ZONE}')
    if not zones:
        raise SystemExit(f'Zone {ZONE} invisible avec ce jeton.')
    zone = zones[0]
    account = zone['account']['id']

    tunnels = api('GET', f'/accounts/{account}/cfd_tunnel?is_deleted=false&name={TUNNEL}')
    if tunnels:
        tunnel_id = tunnels[0]['id']
    else:
        tunnel_id = api('POST', f'/accounts/{account}/cfd_tunnel', {'name': TUNNEL, 'config_src': 'cloudflare'})['id']
        print('Tunnel cree.')

    # Cloudflare ne laisse passer que l'API de collecte ; le serveur refait le meme tri.
    api('PUT', f'/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations', {'config': {'ingress': [
        {'hostname': HOST, 'path': r'^/api/(t|vote|health)$', 'service': 'http://app:8080'},
        {'service': 'http_status:404'},
    ]}})
    print('Regles du tunnel a jour.')

    connector = api('GET', f'/accounts/{account}/cfd_tunnel/{tunnel_id}/token')
    old = os.umask(0o077)
    try:
        with open(TOKEN_FILE, 'w') as f:
            f.write(connector)
    finally:
        os.umask(old)
    print('Jeton du connecteur range dans ~/.config/apercu-panneau.')

    target = f'{tunnel_id}.cfargotunnel.com'
    records = api('GET', f"/zones/{zone['id']}/dns_records?name={HOST}")
    if not records:
        api('POST', f"/zones/{zone['id']}/dns_records",
            {'type': 'CNAME', 'name': HOST, 'content': target, 'proxied': True,
             'comment': "Avis et telemetrie de la page d'essai du panneau LeClasseur (mad-server)"})
        print('Adresse DNS creee.')
    elif records[0]['content'] != target:
        raise SystemExit(f'{HOST} pointe deja ailleurs ({records[0]["type"]} {records[0]["content"]}) : rien change.')
    print(f'{HOST} -> tunnel {TUNNEL}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
