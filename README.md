# Scacchackathon

Tournament server for a one-day chess engine hackathon.

- Players submit engines (any language, packed as a **Docker image**, speaking **UCI**) with a personal API token.
- Each upload is built and smoke-tested right away. The player sees a `READY` or a clear error.
- The organizer opens submissions and starts evaluation rounds from the **admin panel**.
- A round is a [fastchess](https://github.com/Disservin/fastchess) round-robin between each player's latest ready version, using the 8moves_v3 opening book with colours swapped.
- The public site shows the leaderboard (points, W/D/L, Elo ±, cross table, history over rounds), live games and a replay viewer for every game. The viewer has optional Stockfish 19 analysis (best-move arrows, eval bar, top two lines) that runs in the viewer's browser (lite WASM build, ~1.9 MB, bundled into the image at build time), so it never takes CPU from the tournament.

## How it works

```
player ──submit.sh──▶ POST /api/submit ──▶ docker build ──▶ UCI smoke test ──▶ ready
admin ──"Start round"──▶ fastchess ──docker run -i --network none --cpus 1 ...──▶ engines
                            │ events.ndjson (every move)
                            ▼
                  SQLite ──▶ leaderboard / live boards / game viewer
```

One container (`app`) runs FastAPI, the build queue and fastchess. It drives the **host's Docker** through
`/var/run/docker.sock`, so engine images and game containers are siblings on the host. Everything is stored in `./data`.

| Path | What |
|---|---|
| `app/main.py` | routes: public pages, player API, admin |
| `app/builder.py` | extract upload → `docker build` → UCI smoke test |
| `app/runner.py` | builds the fastchess command, tails its event stream into the db |
| `app/ratings.py` | standings, Sonneborn-Berger, Bradley-Terry Elo |
| `client/submit.sh` | player upload script (served at `/submit.sh` with the URL filled in) |
| `engines/` | starter engines (random, minimax), downloadable from `/docs` |
| `deploy/` | VM setup + deploy scripts |

## Run locally

```bash
cp .env.example .env    # set ADMIN_PASSWORD
docker compose up -d --build
open http://localhost:8000
```

## Deploy on a VM (Nebius)

1. Create an Ubuntu 22.04/24.04 CPU VM with a public IP. **16 vCPU** is a good size: 7 players at 10+0.1 with 8 games in parallel take about 20 minutes per round. Allow inbound TCP 22, 80 and 443 (or 8000).
2. Copy the setup script to the VM and run it:
   ```bash
   scp deploy/setup-vm.sh ubuntu@VM_IP: && ssh ubuntu@VM_IP ./setup-vm.sh
   ```
3. On the VM, create `/opt/scacchackathon/.env` from `.env.example`:
   - `ADMIN_PASSWORD`, `SECRET_KEY` (`openssl rand -hex 32`)
   - `PUBLIC_URL=http://VM_IP:8000` and `BIND=0.0.0.0`, or with a domain: `DOMAIN=chess.example.com`, `PUBLIC_URL=https://chess.example.com`, `BIND=127.0.0.1`
4. From your laptop: `deploy/deploy.sh ubuntu@VM_IP`.
   With a domain, also run `docker compose --profile https up -d` on the VM.

## Runbook for the day

1. **Admin → Players:** add each friend. Their token is shown **once**, so send it to them privately ("New token" replaces it).
2. **Admin → Open submissions.** Friends run `./submit.sh path/to/engine` and watch the build log.
3. When everyone is ready: **Start evaluation round**. This closes submissions and starts the round-robin.
   Each player's latest `ready` version plays. Builds still running are skipped (the admin page warns you).
4. Watch the leaderboard and live boards on the projector. When the round finishes, open submissions again for the next iteration.
5. For the final, consider more openings per pairing (Settings → e.g. 20) for tighter error bars.

**Settings** (admin, applied to the next round): time control, openings per pairing, games in parallel (0 = CPUs/2),
CPU and memory per engine, time margin, max moves, score-based adjudication.

**When something goes wrong**

- A broken version made it in: on the admin page, **Disable** it. The previous ready version is used instead.
- A round is stuck: **Cancel round**. Engine containers are cleaned up automatically. Check the round's **log** link (fastchess output).
- An engine keeps losing on time: try a larger time margin, or ask the player to budget their time better.
- If the app restarts, a running round is marked `failed` and pending builds are retried. Start a new round.
- Logs: `docker compose logs -f app` · build logs: admin → submission → log.
- Free disk after the event: `docker image prune -a --filter label=scacc.engine=1`.

## Security notes

Engines run with `--network none`, CPU/memory/pid limits and `--rm`. The *build* has network access and runs
players' Dockerfiles on the host's Docker daemon. That's fine among friends, but this isn't a sandbox for strangers. The app
container controls the host's Docker, so only expose the admin password to the organizer.
