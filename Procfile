web: uvicorn app.distributed.heroku:create_app --factory --host 0.0.0.0 --port $PORT --no-access-log --no-proxy-headers --timeout-keep-alive 95
dispatcher: python -m app.distributed.dispatcher
release: python -m app.distributed.migrate
