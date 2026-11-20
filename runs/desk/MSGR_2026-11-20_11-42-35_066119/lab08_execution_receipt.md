# Lab 08 execution receipt

Transcription of already observed execution output supplied by the user in this session. This receipt records prior executions; no workflow or test was newly executed to create it.

## Replay
Command:
```text
python -B -u one_desk.py --replay "runs/desk/MSGR_2026-11-20_11-42-35_066119"
```

Observed output:
```text
Fixed execution order (saved-file replay): validate evidence -> numeric hook -> reproduce final report
Desk report: C:\Users\franc\equity-analyst\runs\desk\MSGR_2026-11-20_11-42-35_066119\desk_report.md
Replay elapsed seconds: 1.128
```

## Saved-input screen
Command:
```text
python -B -u one_desk.py screen --saved-screen "spinoff_runs/report_2026_11_12T11_21_24_594118_05_00.md" --reuse-desk "runs/desk/MSGR_2026-11-20_11-42-35_066119"
```

Observed output:
```text
Fixed execution order: Seeker -> Selection -> Agent team -> Worksheet -> Numeric hook -> Final report (Seeker runs only for screen)
Saved-input screen execution: local saved evidence; not a fresh live screen.
[desk] Seeker
[desk] Reusing completed seeker report: C:\Users\franc\equity-analyst\spinoff_runs\report_2026_11_12T11_21_24_594118_05_00.md
[desk] Seeker elapsed seconds: 0.265
[desk] Selection
[desk] Selected ticker: MSGR
[desk] Selection elapsed seconds: 0.005
[desk] Agent team
[desk] Reusing saved team evidence: C:\Users\franc\equity-analyst\runs\desk\MSGR_2026-11-20_11-42-35_066119\verified_report.md
[desk] Agent team elapsed seconds: 0.000
[desk] Worksheet
[desk] Reusing saved worksheet: C:\Users\franc\equity-analyst\runs\desk\MSGR_2026-11-20_11-42-35_066119\spinoff_worksheet_20261120_114503_927200_12483578.md
[desk] Worksheet elapsed seconds: 0.001
[desk] Numeric hook
[desk] Numeric hook elapsed seconds: 0.454
[desk] Final report
Desk report: C:\Users\franc\equity-analyst\runs\desk\MSGR_2026-11-20_11-42-35_066119\desk_report.md
[desk] Final report elapsed seconds: 0.855
```
