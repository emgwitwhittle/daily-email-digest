name: Daily Email Digest

on:
  schedule:
    - cron: '0 15 * * *'   # 7:00 AM Pacific (15:00 UTC)
  workflow_dispatch:         # allows manual trigger from GitHub

jobs:
  run-digest:
    runs-on: ubuntu-latest

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Restore Gmail token
        run: echo '${{ secrets.GMAIL_TOKEN }}' > token.json

      - name: Run digest
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python digest.py

