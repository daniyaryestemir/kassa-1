from app import app

app.run(
    host="0.0.0.0",
    port=5443,
    debug=False,
    use_reloader=False,
    ssl_context="adhoc",
)
