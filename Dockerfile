# Mech Royale bot "ghost-v1": standard library only, so nothing to install.
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /bot
COPY mechfight.py nav.py ghost_bot.py /bot/
ENTRYPOINT ["python3", "-u", "/bot/ghost_bot.py"]
