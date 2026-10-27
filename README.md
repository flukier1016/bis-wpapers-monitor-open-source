# BIS Working Papers Monitor

An independent, self-hosted monitor for [BIS Working Papers](https://www.bis.org/publications/working-paper). It indexes new papers, ranks them with configurable rules, sends optional email digests, and keeps a Markdown archive and searchable web dashboard. This project is not affiliated with or endorsed by the BIS.

## Getting started

Requires Python 3.11 or later. For example, with [uv](https://docs.astral.sh/uv/):

```sh
uv venv --python 3.12
uv pip install -r requirements-dev.txt
cp .env.example .env
./run_local.sh
```

Open <http://127.0.0.1:8099>. The local launcher disables the automatic startup run; set `RUN_ON_STARTUP=true` to enable it. On the first check the monitor seeds its baseline and does not email old papers. This initial operation contacts BIS and can take time.

To run a manual check without sending email, set a strong `ADMIN_TOKEN` in `.env` and use the admin page or:

```sh
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  'http://127.0.0.1:8099/admin/run?send_email=false'
```

An environment variable in your shell is separate from `.env`; export `ADMIN_TOKEN` before using the example above. Admin endpoints are disabled if the token is unset. Use the request header, never a URL query string, because URLs can be logged. Use HTTPS when connecting remotely.

## Configuration and deployment

See [`.env.example`](.env.example) for configuration. Without `DATA_DIR`, the database and archive use `./data`; keep this directory private and persistent. When `DATA_DIR` is explicitly set, it must already exist, to guard against a missing volume. In production, mount a persistent volume (for example `/data`) and set `DATA_DIR` to its mount point. Back up the volume: losing it loses the deduplication state. Set `PUBLIC_URL`, a strong `ADMIN_TOKEN`, time zone and SMTP settings as appropriate. Never commit credentials or production data.

Email is optional. With SMTP configured, a failed send leaves papers unreported for retry. With SMTP unconfigured, the archive and dashboard are treated as the delivery channel. Dry runs do not mark papers reported. A crash in the send/confirmation window creates unavoidable delivery ambiguity; exactly-once email delivery cannot be guaranteed.

For hosts without SMTP access, `EXTERNAL_MAILER=true` disables the in-process schedule and delegates delivery to the included GitHub Actions workflow. Set `GITHUB_REPO`, repository variable `BIS_MONITOR_URL`, and secrets `BIS_ADMIN_TOKEN`, `SMTP_USER`, and `SMTP_PASSWORD`. The workflow schedule uses UTC; adjust its cron to match your intended local time. Workflow logs and run summaries may contain operational information; review repository visibility and retention before enabling it. Never share tokens, recipient lists, or personal archives in a public repository.

## How it works

The app walks the paginated BIS publications listing and queries paper pages, stores identifiers in SQLite, and compares each list with the existing baseline. Minimum-list-size checks help detect incomplete responses. Priorities combine JEL classifications, topic keywords, abstract completeness and page count; see [`app/config.py`](app/config.py) for weights. Rankings express system rules, not the authors' views.

The BIS website can change without notice, which may break retrieval. A full check currently requests about 140 listing pages, using a configurable delay; if the advertised total changes or the parsed count is incomplete, the check fails without changing the baseline. BIS also lists supplemental appendices without a working-paper number; those are counted for completeness but are not treated as separate papers. Duplicate series numbers are combined only when dates match and either titles or author sets agree; ambiguous conflicts stop the check. Tests use saved page fixtures; they detect regressions against those snapshots but do **not** detect an upstream change by themselves. Review service logs and verify live behavior separately.

The dashboard provides `/`, `/paper/{series}`, `/runs`, `/thesis`, `/digest/latest.md`, `/api/papers.json`, and `/healthz`. The optional `/thesis` collection contains existing **Chinese-language research notes**. Their review statuses describe the project's checks, not a guarantee of accuracy. Verify figures and attribution against the original paper before citing. Notes are unofficial, not BIS translations, and the BIS does not guarantee their accuracy or completeness. Each note links to its original source when the corresponding paper is available.

## Tests

```sh
.venv/bin/python -m pytest
```

No live email or production service is needed for the test suite.

## License and third-party material

Project code is available under the [MIT License](LICENSE). BIS source material, including saved BIS page fixtures and retrieved abstracts, remains subject to [BIS terms and conditions](https://www.bis.org/about/terms-conditions); the MIT license does not grant rights in third-party material. Users are responsible for ensuring their use, redistribution, and translations meet those terms. Research notes are analytical commentary and are not official BIS translations.
