# Costs

## How the bill is built
- **GPU machines.** Billed per second (with a minimum, configurable as `min_billing_s`,
  default 5 min) from creation until destroyed. Powered-off machines **are still billed**, so
  aivo always destroys them.
- **Typical creation cost** with the cold start included. Numbers are rough; the dashboard
  shows a live estimate:

  | Creation | Machine | Cost |
  |---|---|---|
  | One Draft image | RTX 4000 Ada | ~$0.10–0.20 |
  | A 5 s Standard video | 48 GB tier | ~$0.40–0.60 |
  | Follow-up work on a machine that's already running | — | cents |

- **The idle tail.** After the last creation the machine stays on for the idle timeout
  (default 10 min) in case more work comes. That costs at most ~$0.13 (RTX 4000 Ada), ~$0.26
  (48 GB) or ~$0.74 (H100). Lower it in Settings if you mostly make one-offs. Raise it, or use
  *Keep warm*, for sessions.

## How the machine is chosen
For each candidate the scorer estimates:
- **wait** = create + boot + model load + generation + download;
- **cost** = price × (billable time incl. the minimum and the expected idle tail), shared across
  queued compatible jobs.

It then minimizes `cost + λ × wait`. The λ values are what an hour of waiting is worth:

| Priority | λ ($ per hour of waiting) |
|---|---|
| Save money | $1 |
| Balanced | $4 |
| Fastest | $20 |

Small penalties are added for machines that were often sold out lately, for far more GPU than
needed, and for lighter model variants. Estimates start from priors and are replaced by the
timings measured on your own runs.

## Cache costs (monthly, "Balanced" policy)

| Item | Cost |
|---|---|
| Prepared machine image | ~$1.50–2 per region × 2 regions |
| Spaces model mirror | $5 (250 GB included) |
| One 120 GB model disk (TOR1) | ~$12 |
| **Total** | **~$20/month** |

Turn pieces off in Settings → Faster start-up to save money.

## Limits
- **Warning level** (`soft_cap_usd`, default $30/month): users confirm each new creation.
- **Spending limit** (`hard_cap_usd`, default $40/month):
  - Placement reserves budget atomically, so concurrent requests can't overshoot.
  - At the limit, new machines stop starting and running ones finish their current job.
  - At the limit plus $2 they are destroyed immediately.
- The provider's month-to-date figure is shown next to aivo's own accounting. A billing alert
  in the DO control panel is recommended.
