# CampusSync Agent (iteration 5)

A plug-in scheduling agent for campus systems. When a student or faculty member mails the other asking to meet, the agent checks **everyone's** timetables (from the portal), exams, holidays, uploaded commitments and booked meetings, then drafts the reply: "yes, that works" (and books it) or "that time doesn't work, here's when we're all free". It adds the meeting to everyone's calendar and sets reminders.

It talks to VTOP through a **connector**, so the same agent can be plugged into another college or system by swapping connectors and a settings file. **Mail is real Gmail**: the first thing the app asks is to connect the Gmail of whoever runs it. The college side (VTOP timetables, attendance, academic calendar, exams) and the calendar are **mock backends**.

## What it does

| Feature | Where to see it |
|---|---|
| Reads meeting mails (specific time, a day, "morning", a range, "this/next week", "when are you free", urgent, reschedule, cancel, "option 2 works"), including messy ones like "sir tmrw 4 ok?" | Inbox |
| Checks everyone's timetables and drafts a booking or common free options; the draft is ready when you open the mail | Inbox |
| Offered options are held for 4 hours so nobody else grabs them | Inbox, Calendar > Week (dotted blocks) |
| Group meetings: "our team" pulls in all teammates; if no time fits everyone, options that miss just one person | Inbox |
| Books the meeting, adds it to everyone's calendar, schedules reminders, .ics download | Inbox, Calendar > Upcoming |
| Cancel any meeting, everyone told | Calendar > Upcoming |
| Attendance-aware: never books over a class a student can't afford to skip (75% rule, 9+ CGPA exempt) | Calendar > Week (dashed = skippable), Calendar > Attendance |
| Exams, exam prep buffer, holidays and exam weeks pulled from the academic calendar and blocked | Calendar > Week |
| Cabin-hours queue: students grab 10-min tokens in faculty cabin hours; full session > waitlist; a cancelled token goes to the next student automatically | Cabin hours |
| Add busy times the portal doesn't know (plain words, form, .ics, or a pasted timetable) | Calendar > Busy times |
| Clash alerts the moment an added busy time overlaps a class, exam or meeting, with one-click fix | Calendar > Busy times, Home |
| Mass reschedule when someone can't make it on a day, everyone notified | Calendar > Upcoming > I can't make it on a day |
| Plain questions: "when is my guide free this week?", attendance, exams, holidays, meetings; book from the answer | Assistant |
| Per-person preferences: not before/after, max meetings a day, gap between meetings, no-meeting days | Settings > Preferences |
| Faculty numbers: meetings by day, cabin-hour use, median reply time, all-faculty table | Home > Your week in numbers |
| Telegram pings for bookings, options, reminders and moved meetings | Settings > Notifications |
| Leave, the VIT way: parent mails the proctor, student applies on VTOP (mocked), proctor approves. The agent matches the two, shows classes missed on working days, attendance after, exams and meetings in the window; on approval it blocks the time, tells course faculty, moves meetings, and replies to student and parent | Students > Leave requests (faculty), Leave (students, parents) |
| Mail sorting by DeepSeek with categories it can add; deadlines in mail go into the deadline list automatically | Inbox filter, Calendar > Deadlines, Settings > Mail categories |
| Daily summary: what needs a reply, today, deadlines this week, alerts; mail it to yourself | Home |
| Proctor check-ins: risk per proctee from attendance and time since last check-in; plan and book them all in one go | Students > Proctor check-ins |
| Small neural network that double-checks the intent of every mail; hybrid retrieval for the Assistant; evaluation sets | Settings > Demo and system |
| Auto mode: replies and books without approval | Settings > Demo and system |
| Privacy: replies never say why someone is busy; busy times only visible to their owner | everywhere |
| Second college on a CSV export, no code changes | `config/settings.other_college.yaml` |

## Run it

Needs Python 3.10 or newer.

```bash
cd campussync-agent
pip install -r requirements.txt
streamlit run app.py
```

### Connecting Gmail (first screen)

The app opens on **Connect your Gmail**. Nobody's account is built in; each person signs in with their own.

**Option A: Sign in with Google (recommended).** Google requires every app that reads Gmail to have an "OAuth client". It's created once per copy of the app, by anyone on the team, and it identifies the app, not a mailbox:

1. [console.cloud.google.com](https://console.cloud.google.com/) -> create a project.
2. APIs & Services -> Library -> **Gmail API** -> Enable.
3. APIs & Services -> OAuth consent screen -> **External** -> app name + your email -> Save. Add every Gmail that will sign in under **Test users**.
4. Credentials -> Create credentials -> **OAuth client ID** -> type **Desktop app** -> Download JSON.
5. Upload that JSON on the connect screen, then click **Sign in with Google**. Google opens in a new tab; after you allow access the app continues there.

While the project is in Testing, Google shows "app isn't verified" (click Continue) and sign-ins expire after 7 days (just connect again). The app runs at `http://localhost:8501/`; if you use another port, change `gmail.redirect_uri` in `config/settings.yaml`.

**Option B: Gmail app password.** No Google Cloud. Needs 2-Step Verification and an app password from [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). College Workspace accounts often block this.

**Then pick your VTOP profile.** The college side is mocked, so you choose which mock profile's timetable, attendance and cabin hours are yours, and the name replies are signed with.

What it can do with your Gmail: read inbox mail from the last 14 days, send replies you approve (threaded under the original), and add a `CampusSync` label to mails it has handled. It never deletes mail. Sign-in files stay in `data/secrets/` on your computer (git-ignored, not in the zip).

**How real and mock mix:**
- Mail in your Gmail inbox shows up in the app's Inbox (filter: Gmail / Demo mail / All). The agent checks every minute while the app is open, or click *Check for new mail* in the sidebar.
- A sender who isn't linked to VTOP is an outside contact: the agent checks only your timetable. Link their address to a VTOP profile (in the mail under *Link this sender to VTOP*, or Settings > Mail and contacts) and it checks theirs too.
- Replies you approve go out from your Gmail. Anything addressed to mock VTOP profiles (`.mock` addresses) stays inside the app, so the leave, proctor and cabin-hour demos still work without mailing anyone real.
- Two teammates can each run the app with their own Gmail, link each other's address to VTOP profiles, and have their agents schedule over real mail.
- Auto mode with Gmail connected sends real replies without review, so it's off by default.
- *Skip for now and use the demo mailbox* on the connect screen runs everything on mock mail (handy if Google sign-in fails during a review).

On first run it generates mock VTOP data for today (6 faculty, 20 students, their 20 parents and the school office; FFCS timetables, attendance, project teams, cabin hours, CAT-2/FAT, holidays) plus 11 demo mails and 3 leave cases at different stages. It also trains the intent network once (about 3 seconds).

**DeepSeek (optional):** copy `.env.example` to `.env` and put your key in `DEEPSEEK_API_KEY`, or paste it under Settings > Demo and system. Without a key everything still works with the built-in parser and reply templates.

**Telegram (optional):** create a bot with @BotFather, put the token in `.env` as `TELEGRAM_BOT_TOKEN`, message your bot once, open `https://api.telegram.org/bot<token>/getUpdates` to find your chat id, and paste it in Settings > Notifications. Without a token the pings are only written to the log there.

Other ways to run:
- `python scripts/demo.py` - command-line walkthrough of every feature
- `python scripts/evaluate_parser.py --failures` - mail-understanding scores (adds DeepSeek automatically when the key is set)
- `python scripts/evaluate_retrieval.py --misses` - retrieval scores per backend
- `python scripts/evaluate_triage.py --wrong` - mail sorting scores, rules vs DeepSeek
- `python -m pytest -q` - 109 tests (core logic, mail flows, holds, groups, preferences, cabin queue, timetable import, dashboard, leave procedure, triage, daily summary, proctor check-ins, Gmail sync/send/threading/labels with a fake account, the Gmail API client against recorded API responses, the IMAP/SMTP client, the Google sign-in flow, Telegram and DeepSeek paths with fakes, the CSV college, a 2,000-meeting speed check, and headless click-throughs of every page for each role, plus the connect screen)
- `CAMPUSSYNC_CONFIG=config/settings.other_college.yaml streamlit run app.py` - same agent, different college (Windows PowerShell: `$env:CAMPUSSYNC_CONFIG="config/settings.other_college.yaml"; streamlit run app.py`)

## Iteration 2: what changed and the numbers

| Area | Before | After | How it was measured |
|---|---|---|---|
| Speed with 2,000 meetings (20 slot searches) | 11.8 s | 0.19 s | `tests/test_iteration2.py::test_scales_to_thousands_of_meetings`; per-person indexes in the store, cached type hints, memoised busy blocks |
| Mails fully understood, **holdout set** (21 mails never used for tuning) | 50% | 86% | `data/eval/mails_holdout.jsonl` (the leave mail was added before the leave rules were written) |
| Mails fully understood, main set (42 mails, rules tuned on it) | 78% | 100% | `data/eval/mails.jsonl` (optimistic: tuned on it) |
| Retrieval: right document in top 3 (16 questions) | 81% | 94% hybrid, 100% dense | `data/eval/retrieval.jsonl` (small, and I tuned the synonym list on it) |

The holdout number is the honest one. Remaining misses on it: thank-you mails that mention "doubt" or "review" get read as meeting requests, and "move tomorrow's meeting to the evening" loses the date.

Other fixes: "this week / next week" in a mail is now a real search range (before, it searched from today); group replies say "Hi all" and don't claim "I'm not free" when a teammate is the busy one; a stale-cache case after a demo reset was closed.

## Mail sorting with DeepSeek

Every mail is sorted once by DeepSeek (in batches of 8) into a category, a one-line summary, whether it needs a reply, a deadline and a priority.

- Core categories are always there because the app acts on them: Meeting, Leave request, Deadline, Question, Announcement, Update, Promotion.
- Hard rules on top of whatever DeepSeek says, so the inbox stays calm:
  - **Needs reply** only for mail a person wrote to you and expects an answer to. Mail to a group or mailing list (a Google group "via" post, List-Id/List-Unsubscribe headers, many recipients, "Dear all") never needs a reply, and is never treated as a meeting request, so the agent won't draft replies to it or answer it in Auto mode.
  - **Deadline** only for something you owe by a date: coursework, a review, a lab record, fees, a library book. Internships, placement drives, hackathons, events, workshops and course registrations are not deadlines, even with a last date.
  - **Promotions never show up.** Gmail's Promotions and Social tabs are not pulled in at all (`gmail.skip_tabs`), and anything DeepSeek marks as a promotion is hidden. Mail from the college's own domains (`institution.mail_domains`) is never hidden.
  - Mail sorted under older rules is sorted again automatically, and deadlines it wrongly created disappear.
- There is no fixed list beyond those. DeepSeek reuses categories it made before and creates a new short one when a mail fits none ("Placement", "Fee Payment", "Library", "Club Events"...). Near-duplicates ("Placements") are folded into the existing one; at most 12 custom categories (`triage.max_custom_categories`).
- A deadline is kept only if the mail actually states a date.
- The Inbox filter is built from the categories in your mail. Settings > Mail categories lists them and lets you merge one into another (DeepSeek won't recreate a merged one).
- Without a key, or if a call fails, the built-in rules sort into the core categories.
- `python scripts/evaluate_triage.py --wrong` scores rules vs DeepSeek on 30 labelled inbox mails (`data/eval/triage.jsonl`, including real cases like a group hackathon invite and an internship forward). Rules alone: category 73%, needs reply 97%, deadline 97%.

## Iteration 5: a calmer app

The app had grown to 12 tabs with every option on screen at once. Now:

- **Pages in the sidebar, by role.** Faculty see Home, Inbox, Calendar, Students, Cabin hours, Assistant, Settings. Students see Leave instead of Students. Parents see Inbox, Leave and Settings.
- **Home** answers "what needs me today": four numbers, today's schedule, mail waiting for a reply (with Open), deadlines this week and alerts.
- **Inbox works like a mail app**: list on the left, the open mail on the right. For a meeting or leave mail the agent's draft is already there; *Approve and send* is the one main button. Booking an offered time directly, replying yourself and linking a sender are tucked under small menus. New mail opens in a dialog.
- **Calendar** holds what used to be four tabs: Week, Upcoming (with "I can't make it on a day"), Deadlines, Busy times and, for students, Attendance.
- **Settings** gathers preferences, notifications, Gmail and contacts, mail categories, and the demo/system controls that used to fill the sidebar.
- Quieter feedback (short toasts instead of green boxes), one accent colour, day-first dates, no emojis.

## Iteration 4: real Gmail

- Connect screen first: Sign in with Google (OAuth, PKCE, token refresh) or Gmail app password (IMAP/SMTP).
- Profile step: link your Gmail to a mock VTOP profile and set the name replies are signed with.
- Inbox sync every minute, senders mapped to VTOP profiles or outside contacts, replies sent threaded with `In-Reply-To`, handled mails labelled `CampusSync` in Gmail (so a demo reset doesn't re-open them).
- Compose to real addresses; reminders and the daily summary can be copied to your Gmail.
- New code: `adapters/gmail_auth.py`, `adapters/gmail_client.py`, `adapters/gmail_mailbox.py`, `core/directory.py`.

## Iteration 3: leave, triage, proctor check-ins

- **Leave follows the real procedure.** Mails from a student asking for leave get a reply explaining what's missing (VTOP application and/or parent's mail). The approve button stays off until both are in (the proctor can override with a tick box). Attendance impact counts only instructional days from the academic calendar, so holidays, weekends and exam days don't count.
- **No OD.** OD isn't handled by faculty, so it was left out.
- **Triage** runs on every mail when it arrives. Deadline mails ("submit by Monday 5 pm") become entries in each recipient's deadline list, shown on the right day in Calendar > Week.
- **Daily summary** is the Home page, and one click mails it to you. `scripts/` can be extended to send it to everyone on a schedule.
- **Proctor check-ins**: risk is high when any course is below the 75% line. The plan books high-risk proctees first at their earliest common free time, skips anyone who already has one booked, and mails each student.
- All emojis removed from the app.

## 5-minute demo for the review

1. Viewing as **Dr. Kavitha Raman (F101)**, Home shows what's waiting. Open Inbox and pick Diya's *Project report discussion* ("tomorrow at 3 pm"). The agent's draft is already there: she's busy then (only she sees why), so it offers 3 common times. *Approve and send*. The times are now held.
2. Switch to **Diya Krishnan (S002)**. Her inbox has the reply with the times as buttons. Click the first one.
3. Switch back to F101. Open Diya's new mail: the agent books that time for both, with reminders.
4. Still F101, open the *Prototype demo* mail: "our team" pulls in all three teammates automatically.
5. Switch to **Rohan Pillai (S003)** > *Cabin hours* > grab a free token with his guide. Then *Assistant* > "Book 30 min with my guide tomorrow" > tap a time.
6. As a student, *Cabin hours* > Dr. Arjun Menon has a full session > *Join the waitlist*. Switch to a student holding a token there > *Calendar* > *Upcoming* > *Cancel*. The token moves to the waitlisted student.
7. As F101, *Calendar* > *Upcoming* > *I can't make it on a day* > pick the review day > Preview > Move. All project reviews move, each student is mailed.
8. As F101, *Students* > *Leave requests* > Meghna's leave: parent mail received, VTOP application done, classes and attendance impact listed > Approve. Course faculty, the student and the parent are mailed.
9. As F101, *Students* > *Proctor check-ins* > Plan check-ins > Book. At-risk proctees are booked first.
10. As F101, *Inbox* filters (Needs reply, Deadline, Announcement...), then *Home* > *Your week in numbers*.
11. *Settings* > *Demo and system* > *Model accuracy* > *Run the evaluation sets* for the accuracy tables.

## How it's built

```
             mails / questions / uploads / token bookings
                        │
                ┌───────▼────────┐        DeepSeek (optional): reads mails, polishes replies,
                │  Agent layer   │◄────── calls tools for questions. Neural intent model double-
                │ mail handler,  │        checks every mail. Any invented time is thrown away.
                │ assistant      │
                └───────┬────────┘
        ┌───────────────┼──────────────────┬───────────────────┐
┌───────▼──────┐ ┌──────▼───────┐ ┌────────▼────────┐ ┌────────▼────────┐
│ Availability │ │ Clash check, │ │ Hybrid RAG      │ │ Cabin queue,    │
│ engine (free │ │ reschedule,  │ │ (keyword+dense) │ │ analytics,      │
│ slots, holds,│ │ reminders    │ │ over timetables,│ │ timetable       │
│ prefs, attn.)│ │              │ │ policies, etc.  │ │ import          │
└───────┬──────┘ └──────────────┘ └─────────────────┘ └─────────────────┘
        │  connector interfaces (campussync/adapters/base.py)
┌───────▼───────┬──────────────┬───────────────┬──────────────┐
│ Portal        │ Mail         │ Calendar      │ Notifier     │   ← swap these per college/system
│ mock_vtop/csv │ mock_mail    │ mock_calendar │ telegram     │
└───────────────┴──────────────┴───────────────┴──────────────┘
```

The models never decide times. Free slots come from the availability engine; the models only read and write text.

```
app.py                          Streamlit UI (sidebar pages by role)
config/settings.yaml            institution profile: connectors, hours, holds, cabin tokens, attendance, LLM, ML, RAG
campussync/adapters/            connectors: base.py (contract), mock_vtop, csv_portal, mock_mail, mock_calendar,
                                notifier (Telegram), registry, template_adapter (start here for a real one)
campussync/core/                availability, attendance, clashes, reschedule, reminders (+.ics), office_hours,
                                timetable_import, analytics, leave, triage, proctor
campussync/agent/               mail_handler (mail → decision → reply), assistant (questions, tool calling, booking)
campussync/llm/                 client (DeepSeek), parser (model + checks), rules (offline parser)
campussync/ml/                  intent (neural classifier), synth (training mails), evaluate (scoring)
campussync/rag/store.py         retrieval: tfidf | dense | hybrid
data/eval/                      mails.jsonl, mails_holdout.jsonl, retrieval.jsonl
data/knowledge/policies.md      rules the assistant can quote (edit freely)
examples/other_college/         CSV export for the second-college demo
scripts/                        demo.py, evaluate_parser.py, evaluate_retrieval.py
tests/                          pytest suite
```

## Plugging into another college or system

1. Write a connector class implementing `PortalAdapter` (only `list_people`, `get_person`, `weekly_classes` are required; attendance, exams, calendar, cabin hours and teams are optional and switch features on). Start from `adapters/template_adapter.py`.
2. Register it in `adapters/registry.py`.
3. Copy `config/settings.yaml`, set `adapters.portal`, meeting hours, working days, attendance rule.
4. Run with `CAMPUSSYNC_CONFIG=your_settings.yaml`.

## Things to know (honest notes)

- **Not tested against a live Google account.** Google is blocked from the environment this was built in. The Gmail code is tested with a fake account, with recorded Gmail API responses run through Google's own client library, and with the sign-in exchange mocked. First real run: expect to find small things; the connect screen shows Google's error message if something's off.
- **Not tested against live DeepSeek or Telegram.** Both are blocked from the environment I built this in. Their code paths are tested with fakes that behave like the real APIs. Run `python scripts/evaluate_parser.py` with your key first: it scores DeepSeek next to the rules on the same mails.
- **The neural intent model is a small multi-layer perceptron (scikit-learn), not a big deep model.** It trains on about 1,300 generated mails in roughly 3 seconds. On the holdout set it ties the tuned rules; its value is as a safety net for wording the rules don't cover. Say this plainly in the report.
- **Dense retrieval here is LSA** (a linear method), because sentence-transformers needs a model download that was blocked. Install `sentence-transformers` and it switches to real neural embeddings automatically. As far as I know DeepSeek has no embeddings API.
- The timetable paste reader looks for a course code and a slot in each line. I don't know VTOP's exact copy-paste layout, so test it with a real copy of yours.
- The FFCS grid, names, rooms, courses, teams, parents and dates are invented mock data. The VTOP leave application is mocked; a real VTOP connector would read applications from VTOP instead of the app's form.
- Working Saturdays (if VIT declares one) aren't in the mock calendar yet; the real calendar connector would mark them instructional and the leave impact would count them.
- State is a JSON file (`data/state/state.json`). Fine for a demo, not for many users at once.

## Ideas for iteration 3

- Run with the real DeepSeek key and compare against rules + model; fix what it gets wrong
- Fix the two known holdout misses (thank-you mails, "move it to the evening")
- Real connectors: Gmail/Outlook and Google Calendar
- SQLite instead of the JSON file; a small REST API so a Telegram bot or WhatsApp can talk to the agent directly
- Room and lab booking as resources with their own availability
