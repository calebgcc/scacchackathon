# --- fastchess (tournament runner) ---------------------------------------
FROM debian:bookworm-slim AS fastchess
RUN apt-get update && apt-get install -y --no-install-recommends g++ make git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG FASTCHESS_REF=v1.8.3-alpha
RUN git clone --depth 1 --branch ${FASTCHESS_REF} https://github.com/Disservin/fastchess.git /src \
    && cd /src && make -j2 && strip fastchess

# --- opening book + browser assets ---------------------------------------
FROM debian:bookworm-slim AS book
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/book.zip https://github.com/official-stockfish/books/raw/master/8moves_v3.pgn.zip \
    && unzip /tmp/book.zip -d /book && ls -la /book
# Stockfish 19 (lite, single-threaded WASM build, GPLv3) runs in the viewer's browser for game analysis;
# chess.js turns its UCI lines into SAN. Pinned by checksum.
ARG SF_URL=https://github.com/nmrugg/stockfish.js/releases/download/v19.0.1
RUN mkdir /vendor && cd /vendor \
    && curl -fsSL -O ${SF_URL}/stockfish-19-lite-single.js -O ${SF_URL}/stockfish-19-lite-single.wasm \
    && curl -fsSL -o chess.js https://cdn.jsdelivr.net/npm/chess.js@1.4.0/dist/esm/chess.js \
    && printf '%s\n' \
       'ccb130d7ff29408531c47d859a49f91d59d4f482d05a53eb1a3231a4f624c91a  stockfish-19-lite-single.js' \
       'ee4f115bdb2d32cadf96cada104a05b5ec257d7ad6f57582190b49bc71f096de  stockfish-19-lite-single.wasm' \
       '76c7c34f0e2e9ab076521a5d6fe786a9cce537bb1b6f29d32a9c9970b5b232d2  chess.js' \
       | sha256sum -c -

# --- app -----------------------------------------------------------------
FROM python:3.12-slim
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=fastchess /src/fastchess /usr/local/bin/fastchess
COPY --from=book /book/8moves_v3.pgn /opt/book/8moves_v3.pgn
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY --from=book /vendor ./app/static/vendor
COPY client ./client
COPY engines ./engines
ENV DATA_DIR=/data BOOK_PATH=/opt/book/8moves_v3.pgn PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
