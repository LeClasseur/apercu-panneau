# Essai du panneau LeClasseur

Page d'essai (GitHub Pages : https://leclasseur.github.io/apercu-panneau/) montrant le panneau LeClasseur à côté d'un schéma de BOSS, avec :

- **une explication de départ** : se mettre dans la peau d'un courtier qui voit BOSS et le panneau toute la journée ;
- **12 missions chronométrées** : 3 par format, dans un ordre de formats tiré au hasard pour chaque personne. Les sections visées changent selon la position ;
- **un avis enregistré**, un par personne (identifiant aléatoire gardé dans le navigateur). La dernière version compte, et l'historique est gardé ;
- **de la télémétrie anonyme** : temps actif par format, mode et couleur, choix faits à la main, navigation, clics, missions.

Données fictives seulement. Aucun nom requis. Les IP ne sont jamais gardées, seulement un hachage salé, utilisé pour signaler deux avis venant du même réseau.

## Serveur (mad-server)

`server/server.py` utilise seulement la bibliothèque standard Python et SQLite. Les données sont dans `~/.local/share/apercu-panneau/`.

| Porte | Où | Ce qui répond |
|---|---|---|
| publique 8080 | tunnel Cloudflare dédié `apercu-panneau` (seulement `/api/t`, `/api/vote`, `/api/health`) | collecte |
| privée 8081 | `100.118.62.120:8098` (Tailscale) et `127.0.0.1:18098` | `/resultats`, `/api/results` |

```bash
cd ops && docker compose up -d          # app + cloudflared
ZONE=madera.tools HOST=avis.madera.tools \
  ~/laurianne-sylvestre/scripts/avec-secret.sh <CLE_JETON_CLOUDFLARE> CF python3 -I ops/cloudflare.py   # une fois
```

Pour tester en local, ajouter `?api=http://localhost:PORT` à l'adresse de la page : ça remplace l'adresse de collecte.
