# Integration tests

These need a running store, so they are skipped unless `TICK_LAB_TEST_S3=1`.

    # 1. bring up the stack (MinIO node included)
    docker compose up -d

    # 2. load the fixture ticks from your machine
    #
    # tick-lab/tests/fixtures/ also holds the golden comparison CSVs
    # (golden_1m_bars.csv, golden_1m_bars_all.csv) that this same test
    # asserts against -- they don't parse as SYMBOL_trades/quotes... tick
    # files, so `tick-lab load tests/fixtures/` exits 1 on them. Copy just
    # the loadable samples into a scratch dir and load that instead:
    cd tick-lab
    mkdir -p /tmp/tick-lab-fixtures
    cp tests/fixtures/MSFT_trades_sample.txt tests/fixtures/MSFT_quotes_sample.txt \
       tests/fixtures/MSFT_quotes_8field_sample.txt /tmp/tick-lab-fixtures/
    .venv/bin/tick-lab load /tmp/tick-lab-fixtures

    # 3. run the parity test inside the image
    #
    # The Dockerfile never COPYs this repo into the image, and pytest isn't
    # among its runtime dependencies -- `python -m pytest /workspace/...`
    # fails on both counts. Bind-mount the checkout and install pytest for
    # this one-off run instead:
    cd ..
    docker compose run --rm \
      -v "$(pwd):/workspace" \
      -e TICK_LAB_TEST_S3=1 \
      openbb-api sh -c "pip install pytest -q && python -m pytest /workspace/tests/integration -q"
