# Vetting: the Anthropic SDK in the LLM gateway (Phase 2, step 8)

Checked 2026-10-20. Nothing is added to `pyproject.toml` or `uv.lock` until this is reviewed.
DECISIONS 2026-10-16 already chose the provider and the SDK; this page checks the packages that choice
brings in and records SECURITY.md §13's provider due diligence.

## What it is for

One adapter, `AnthropicProvider`, behind the gateway's existing `Provider` interface. Only
`services/llm_gateway` depends on it. The engine's boundary test already refuses `anthropic`; step 8b
adds a repository-wide test that nothing else imports it.

## Packages

Resolved with `uv pip compile` for Python 3.12 on Windows (x86_64) and Linux (x86_64), with
`--exclude-newer 2026-10-06` (the 14-day rule). Both platforms resolve to the same set.

Six packages are new to `uv.lock`. Everything else the SDK needs is already locked at the version the
resolver picked: anyio 4.15.1, pydantic 2.13.5 (and pydantic-core, annotated-types, typing-inspection),
sniffio 1.3.1, typing-extensions 4.16.0, idna 3.20, h11 0.16.0.

| Package | Version (uploaded) | Age on 2026-10-20 | Licence | Publisher | Wheel | PEP 740 provenance | Console scripts |
|---|---|---|---|---|---|---|---|
| anthropic | 1.11.0 (2026-09-30) | 20 days | MIT | Anthropic | pure Python, 1.7 MB (5.7 MB unpacked) | **none published** | none |
| httpx2 | 2.13.1 (2026-09-23) | 27 days | BSD-3-Clause | Pydantic org (author Tom Christie) | pure Python, 96 KB | **none published** | `httpx2` |
| httpcore2 | 2.13.1 (2026-09-23) | 27 days | BSD-3-Clause | Pydantic org | pure Python, 83 KB | **none published** | none |
| jiter | 0.17.0 (2026-09-12) | 38 days | MIT | Pydantic org | compiled (Rust): win_amd64 227 KB, manylinux x86_64 345 KB | GitHub `pydantic/jiter`, `ci.yml` | none |
| docstring-parser | 0.18.0 (2026-04-14) | 189 days | MIT | individual (rr-) | pure Python, 22 KB | GitHub `rr-/docstring_parser`, `build.yml` | none |
| truststore | 0.10.4 (2025-08-12) | 434 days | MIT | individuals (Seth Larson, David Glick) | pure Python, 19 KB | **none published** | none |

The total installed size is about 7.3 MB.

**`opentelemetry-api` is not a dependency of 1.11.0.**
- The requirement `opentelemetry-api<2,>=1.1.0` first appears in **1.14.0** (2026-10-14). The latest
  release (1.17.0) carries it, which is where a reader of PyPI's current page would see it.
- In 1.11.0, the wheel's `METADATA` lists only the seven requirements above, and its source contains
  no reference to `opentelemetry` at all. So this SDK emits no spans, configured or not.
- The package is in the lock anyway, at 1.45.1, through aria_core's tracing setup and the gateway's
  FastAPI instrumentation. For the record:

| Package | Version (uploaded) | Licence | Publisher | Wheel | PEP 740 provenance | In the lock |
|---|---|---|---|---|---|---|
| opentelemetry-api | 1.45.1 (2026-10-06) | Apache-2.0 | OpenTelemetry Authors (CNCF) | pure Python, 60 KB | **none published** | yes, already; unchanged |

- ARIA's own tracer provider (`aria_core.logging.setup.configure_tracing`) exports only to the
  console, or nowhere when `otel_exporter` is `none`. Nothing exports off the machine.
- When the SDK is upgraded past 1.13, it will start creating spans through whatever provider is
  installed. **Step 8b adds two tests**, the same treatment as the onnxruntime telemetry switch-off:
  - **The pinned SDK doesn't import or require OpenTelemetry.** An upgrade that brings it in fails
    this test and comes back for review.
  - **The gateway's tracer provider has no exporter but the console one,** and none at all under
    `otel_exporter=none`. So once the SDK does emit spans, they can't leave the machine.

**`httpx2-jsfetch` 1.0** (2026-08-07, BSD-3-Clause, a 6 KB pure-Python wheel) is in `uv.lock` too, as httpx2's
dependency on `sys_platform == "emscripten"` (Python in a browser). uv records it because the lock is universal;
it is never installed on Windows or Linux. It passes the age gate.

**`idna`:** `httpx2` requires `idna>=3.18`. The lock already holds 3.20, so nothing already locked is
upgraded. Every shared package resolves to the version the lock already has.

- **14-day rule:** every version is at least 20 days old. The newest `anthropic` that passes is 1.11.0.
  Releases 1.12.0 to 1.17.0 came out between 2026-10-07 and 2026-10-20, too recently to use. Each new
  package gets a 14-day `exclude-newer-package` entry.
- **Known vulnerabilities:** `pip-audit --require-hashes` on the resolved set finds none.
- **Install scripts:**
  - Every package ships a wheel for both platforms, so with hashes required nothing is built from source.
  - I opened each wheel as a zip, after checking its SHA-256 against PyPI. None contains a `.pth` file,
    an installer script, a `.data/scripts` entry or any executable.
  - jiter's compiled module (`.pyd` / `.so`) is its only native code. Its wheels carry a provenance
    attestation from pydantic/jiter's own CI.
- **Provenance:** `anthropic`, `httpx2`, `httpcore2` and `truststore` publish no PEP 740 attestation.
  `uv.lock` pins every file's SHA-256, so a changed file is refused. But it can't show that the first
  file was built from the public repository. Each is accepted for its own reason:
  - **`anthropic`:** Anthropic is already inside the trust boundary for this data path. Every byte
    this package handles goes to Anthropic's API, and Anthropic answers it. A compromised SDK
    published by Anthropic would be no worse than a compromised API, which we trust already.
  - **`httpx2` and `httpcore2`:** they don't get that reasoning. They aren't Anthropic's, and they
    carry the API key and every request. They're accepted on four grounds:
    - their custody (see below);
    - a small, pure-Python codebase (about 700 KB unpacked across both) with no install hooks;
    - the hash pins;
    - a review of the pinned version's diff against the release before it, at each upgrade (from
      step 8b on).
  - **`truststore`:** it doesn't see the key or the content. It decides which TLS certificates are
    trusted. It's accepted for its size (19 KB, pure Python), its age (14 months unchanged at this
    version), its two named maintainers, and the hash pin.
- **Activity:**
  - All six repositories are active, and none is archived.
  - anthropic-sdk-python, httpx2 and truststore had pushes on 2026-10-18 to 2026-10-20, jiter on 2026-10-15,
    and docstring_parser on 2026-06-30.
  - Three are owned by organisations (Anthropic, Pydantic). docstring-parser and truststore are single
    maintainers' projects.

### Two things worth knowing

**`anthropic` 1.x brings its own HTTP stack.**
- It depends on `httpx2` and `httpcore2`, not on the `httpx` and `httpcore` the CLI already locks. So
  the lock will hold two HTTP clients.
- The gateway uses `httpx2` only through the SDK. `aria_core/llm_client.py` (step 8c) uses the
  existing `httpx`, so it adds nothing new.
- The alternative was the last 0.x release (0.125.0, 2026-08-19), which uses `httpx`. I recommend
  against it: 0.x is no longer updated, so a CVE fix would arrive only on 1.x, and moving to 1.x then
  would be a forced upgrade under time pressure.

**`httpx2` and `httpcore2` aren't `httpx` under a new version.** A reviewer expecting httpx should
know that both the names and the custody differ.
- The packages are authored by Tom Christie, httpx's original author.
- They're published from **`github.com/pydantic/httpx2`**, under the Pydantic organisation, not
  `encode`. That repository was created on 2026-05-11, is not a GitHub fork of `encode/httpx`, and
  uses its exact description.
- `encode/httpx` is still active (last push 2026-10-02) and is what the CLI locks.
- So the two stacks have different owners, release processes and maintainers, and a fix to one
  doesn't reach the other. I found nothing published by encode about the relationship, so this
  records only what the repositories show.

**`httpx2` trusts the operating system's certificate store** through `truststore`, not certifi's bundle.
- On Windows that means the Windows store, which is what the rest of the machine trusts.
- It also means that a TLS-intercepting proxy trusted by the OS can read gateway traffic. That is true
  of the browser too, and the gateway sends only what §13 allows.

### What the SDK does on its own

I read `anthropic/_client.py` and `_base_client.py` in the 1.11.0 wheel.

- **Request headers.** Every request carries six `X-Stainless-*` headers: language, package version,
  OS, architecture, Python runtime and its version. That's platform metadata, with no content and no
  identifiers. The only per-request user value is the API key.
- **The environment.**
  - Passing `api_key` explicitly turns off every other credential source: `ANTHROPIC_AUTH_TOKEN`, the
    `ant` profiles under `~/.config/anthropic/`, and workload-identity variables.
  - Passing `base_url` explicitly overrides `ANTHROPIC_BASE_URL`.
  - **`ANTHROPIC_CUSTOM_HEADERS` is merged into every request whatever is passed.** That variable could
    add a beta header that turns on a feature outside the data-handling terms below.
  - So the adapter will pass the key and the base URL explicitly, and the gateway will refuse to start
    while `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_BASE_URL` or `ANTHROPIC_PROFILE` is set.
- **Version support.** 1.11.0 has `output_config` (structured outputs: `json_output_format_param`)
  and `inference_geo`. These are the two features step 8 uses.

## Provider due diligence (SECURITY.md §13)

This section records Anthropic's published defaults, each with its source. Some of it depends on how
your organisation is set up, which the code can't see. Those parts are marked **to confirm in the
Console before the first live call**.

### What leaves the machine, by step

These are two separate exposures, and they are answered separately below.

| | Stage 1 model step (step 8) | Bullet writer, 6-second check (steps 9, 10) |
|---|---|---|
| Content sent | the cleaned posting text (third-party, public), fixed instructions, the response schema | the candidate's approved fact texts, metrics and allowed technologies, one requirement, style rules; the top third of the rendered page |
| Candidate data | **none**: no facts, no contact details, no name | **yes**: your own work history, for the first time |
| S2 / S3 | none; the gateway's structural and pattern checks refuse them in both | same |
| Who it is about | the employer | you |

**Stage 1 does not send candidate data.** Its retention exposure is a copy of a public job posting,
held for the retention period below. The posting can contain a recruiter's name or email, so the
gateway's identifier scan of untrusted content still applies.

**Step 9 is the first time your own facts leave the machine.**
- They're minimised per purpose (`PURPOSE_INPUT_KEYS`). That keeps out contact details, visa status
  and the rest of the graph, but the facts themselves are career history.
- The retention answer below applies to them too. Whether that's acceptable is a separate decision,
  made in step 9's plan, not here.

### Published defaults

| Question | Published answer | Source |
|---|---|---|
| Retention of inputs and outputs | **Two Anthropic pages differ.** The platform docs say "Conversation content (your prompts and Claude's outputs) is not retained by default; the exception is Covered Models, which require 30-day retention." The Privacy Center (updated 2026-07-01) says "we automatically delete inputs and outputs on our backend within 30 days of receipt or generation." **We plan on the stricter: up to 30 days.** | [API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention); [How long do you store my organization's data?](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data) |
| Flagged content | Content flagged by automated trust-and-safety systems: inputs and outputs kept up to 2 years, classifier scores up to 7 years. This applies even under ZDR. | both pages above |
| Covered Models | Fable 5, Fable 5.1, Mythos 5 and Mythos 5.1 require at least 30-day retention and can't run under ZDR. **`claude-sonnet-5-5` is not a Covered Model.** | [Covered Models](https://support.claude.com/en/articles/15425695-covered-models) |
| Training | "Retained data is never used for model training without your express permission." Commercial API content isn't used for training by default. | API and data retention |
| Zero data retention | Per organisation, enabled by Anthropic's account team on request (contact sales), not a Console switch. It covers the Messages API. Structured outputs are "Yes (qualified)": prompts and outputs aren't stored, but **the JSON schema is cached for up to 24 hours since last use**. Prompt caching is eligible. Batch processing is not (29 days). | API and data retention, feature table |
| Processing region | `inference_geo: "us"` per request keeps inference in the US, at 1.1× the price; `"global"` (the default) may run anywhere. A workspace can restrict and default it. Data at rest: workspace geo, currently US only. | [Data residency](https://platform.claude.com/docs/en/manage-claude/data-residency); [Pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| Contract (DPA) | The DPA, with Standard Contractual Clauses, is incorporated into the Commercial Terms of Service. Accepting those for API use accepts the DPA, and Anthropic acts as processor. | [View and sign the DPA](https://privacy.claude.com/en/articles/7996862-how-do-i-view-and-sign-your-data-processing-addendum-dpa); [Processor or controller](https://privacy.claude.com/en/articles/9267385-does-anthropic-act-as-a-data-processor-or-controller) |
| Logging on our side | The gateway records metadata only (tenant, purpose, model, tokens, cost, outcome), never the prompt or completion. That's the existing rule. | `services/llm_gateway/aria_llm_gateway/app.py` |

### To confirm in the Console before the first live call

1. **The API key belongs to an organisation under the Commercial Terms**, not a consumer account.
2. **Retention setting** (Console › Settings › Data retention): whether the organisation is on standard
   retention or ZDR. We assume standard, up to 30 days.
3. **Workspace data residency**: `allowed_inference_geos` and `default_inference_geo` for the key's
   workspace. Step 8b defaults to sending `inference_geo: "us"`, so a workspace that disallows it
   would refuse every request.
4. **A spend limit in the Console** as well as the gateway's own caps, so a bug in our accounting isn't
   the only limit.

### Pricing for the gateway's table

`claude-sonnet-5-5`, per million tokens: input $2, output $10, 5-minute cache write $2.50, 1-hour
cache write $4, cache read $0.10. `inference_geo: "us"` multiplies all of them by 1.1. Sonnet 5.5
thinks by default, and thinking is billed as output, so Stage 1 runs at effort `low`. Source:
[Pricing](https://platform.claude.com/docs/en/about-claude/pricing), read 2026-10-20.

## Decisions (approved by Dileep, 2026-10-20)

1. **Add the six packages** at the versions above, to `services/llm_gateway` only. They're added in
   step 8b, with their 14-day `exclude-newer-package` entries.
2. **Structured outputs:** use `output_config.format` with the response schema, as you approved. One
   consequence goes into the §13 entry: the schema is cached by Anthropic for up to 24 hours. It
   holds no candidate data, only field names and descriptions written by us.
3. **No server-side refusal fallback.** Anthropic's guidance turns on `fallbacks` for Sonnet 5.5 by
   default, so a refused request is re-run on another model. It is not used. The
   fallback model would be one we haven't priced, and the gateway refuses unpriced models. It would
   also make a second model part of the data path. A refusal on Stage 1 gives the rules-only
   reading with a note; on step 9, the facts' own words.
4. **`inference_geo: "us"` by default**, as a setting. The 1.1× price is negligible at Stage 1's
   volume, and keeping inference in the US is worth being able to state.
