# Mech Royale bot (team Segfault). Standard library only.
FROM python:3.13-slim
WORKDIR /bot
COPY . .
RUN python3 -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('/bot', quiet=1) else 1)"
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
