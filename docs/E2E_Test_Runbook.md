# Commitment Tracker — End-to-End Test Runbook

**Commitment Tracker {{VERSION}} · end-to-end test · Prepared {{DATE}}**

**From a recorded meeting to an assigned, tracked task.** One full run using free personal accounts only: Fathom records the meeting, Google Gemini (free tier) extracts the tasks, the People directory assigns each one, Google Sheets tracks them in both directions, and Gmail sends the reminders.

**Flow:** Zoom / Meet meeting → Fathom → ngrok → App + Gemini → People directory → Google Sheet ⇄ + Gmail nudge

| Your details | Value |
|---|---|
| App URL (shared test server) | `https://drowsily-posture-unmoving.ngrok-free.dev` |
| Host Google account (share your Sheet with it) | `vaibhavbhasin95@gmail.com` |
| Your sign-in email | Your own address. Use the same one in every step below |

Tick each box as you go. Part 0 is done by the host (developer); testers start at Part 0B.

## Part 0 — Start the app and the public URL (host only)

Two terminals, left running for the whole test.

☐ **1. Start the app** from `C:\PersonalDevelopment\commitment-tracker` with the project's Python:

```
$env:CT_COOKIE_SECURE = "1"
$env:CT_PUBLIC_URL = "https://drowsily-posture-unmoving.ngrok-free.dev"
$env:NUDGE_CHECK_INTERVAL_SECONDS = "60"
python src\app.py
```

- The interval setting makes automatic nudges check every minute instead of every 15, which is handy for testing.
- Keep `secret.key` backed up. Without it, every saved credential has to be re-entered.

☐ **2. Start ngrok** in a second terminal with the static domain:

```
ngrok http --url=drowsily-posture-unmoving.ngrok-free.dev 5000
```

## Part 0B — Sign in (every tester)

☐ **1.** Open `https://drowsily-posture-unmoving.ngrok-free.dev`. On ngrok's "You are about to visit…" page, click **Visit Site**.

☐ **2.** Choose **Create account**: your email and a password of at least 10 characters. Use this same address everywhere below. Your webhook URLs and Sheet script are tied to your account.

## Part 1 — Google Gemini for extraction

aistudio.google.com · the app's **AI extraction** page. Gemini's free tier needs only a Google account: no credit card and no billing setup.

☐ **1.** Go to **aistudio.google.com** and sign in with your Google account. Accept the terms if asked.

☐ **2.** Click **Get API key → Create API key**. If it asks for a Google Cloud project, let it create one. Copy the key (it starts with `AIza`). Leave the project on the free tier; don't click "Set up billing" or "Upgrade".

☐ **3.** In AI Studio, check which **Flash** models are listed, e.g. `gemini-2.5-flash`. Flash models have higher free daily limits than Pro models and are plenty good for this task.

☐ **4.** In the app, open **AI extraction** in the sidebar:

- **Engine:** OpenAI-compatible API (OpenAI, Azure OpenAI, Gemini, Groq, Ollama…).
- **Provider:** Google Gemini. This fills in the base URL `https://generativelanguage.googleapis.com/v1beta/openai`.
- **API key:** paste your AI Studio key.
- **Model / deployment name:** the Flash model name exactly as AI Studio shows it, e.g. `gemini-2.5-flash`.
- Tick the consent box, then **Save**.

☐ **5.** Click **Test connection**. It should list commitments found in a 4-line sample transcript. If Gemini ever fails (for example after hitting the free daily limit), the app quietly uses the built-in model instead. The run history on this page shows which engine was used.

> **Free-tier privacy:** on Gemini's free tier, Google may use your prompts to improve its products. That's fine for test meetings, but don't send transcripts of real confidential meetings through it.

## Part 2 — People directory

So each task gets an owner's email. Owners are matched by name, so the names said in the meeting must match this list.

☐ **1.** On the **People** page, click **Download a template**. Its columns are `First Name, Last Name, Email ID, Corporate User Name, Designation`.

☐ **2.** Fill in 2–3 people. To test alone, use **Gmail plus-addresses** of your own Gmail (replace `you` with your Gmail name); mail to them all arrives in your own inbox:

```
First Name,Last Name,Email ID,Corporate User Name,Designation
Priya,Shah,you+priya@gmail.com,pshah,VP Finance
Arjun,Mehta,you+arjun@gmail.com,amehta,Intern
```

☐ **3.** Upload the file. Two people with the same first name get flagged rather than guessed, so use distinct first names for this test.

## Part 3 — Email nudges with Gmail

Google Account · the app's **Integrations** page.

☐ **1.** In your Google Account, turn on **2-Step Verification** if it isn't on already.

☐ **2.** Go to **myaccount.google.com/apppasswords** and create an app password named "Commitment Tracker". Copy the 16-character code.

☐ **3.** In the app, open **Integrations → Nudges**:

- Choose the **Email** channel. **SMTP server:** `smtp.gmail.com`, **Port:** `587`, **Security:** STARTTLS.
- **User name** and **Send from:** your Gmail address.
- **Password / app password:** the app password, not your normal Gmail password.
- **Cadence:** "1 day before due". Then **Save channel**.

☐ **4.** Click **Send test** on the saved channel. A sample reminder should arrive in your inbox.

☐ **5.** Switch on **Automatic nudges**.

## Part 4 — Google Sheets, both directions

Google Sheets · Apps Script · the app's **Integrations** page.

**From the app to the Sheet**

☐ **1. Create a blank Google Sheet** in your own Google account.

☐ **2. Share it** with `vaibhavbhasin95@gmail.com` as **Editor**. The app writes to sheets through the host's Google login, so it needs this access.

☐ **3. Connect it:** copy the sheet ID from its URL (the part between `/d/` and `/edit`). In **Integrations → Task tracker**, choose **Google Sheets**, paste the ID, **tick Auto-sync**, and **Save**. Auto-sync is what pushes new tasks and every edit to the Sheet automatically.

**From the Sheet back to the app**

☐ **4.** On the same Integrations page, **copy the Apps Script** shown under the Task tracker. It already contains your personal webhook URL and signing secret, so don't share it. In the Sheet, open **Extensions → Apps Script**, delete everything in the editor, paste the script and save.

☐ **5. Add the trigger:**

- Click the clock icon (**Triggers**) → **Add Trigger**.
- **Function:** `onStatusEdit`. **Event source:** From spreadsheet. **Event type:** On edit.
- Approve the permission prompt. Google warns that the app is "unverified" because it's your own script; that's expected.

It has to be an installable trigger like this, because a plain `onEdit` script can't call outside services.

☐ **6. Test the connection:** in the Apps Script editor, select `testStatusEdit` and click **Run**. The execution log should show a `200` response.

> If the signing secret is ever **rotated** (Integrations → Webhook security), the old script stops working. Copy the new script from Integrations and paste it again.

## Part 5 — Fathom to record the meeting

fathom.video · the app's **Integrations** page.

☐ **1.** Sign up free at **fathom.video** with your Gmail address. Connect Google Calendar, plus Zoom if you're using Zoom. Fathom's bot joins scheduled meetings on that calendar.

☐ **2.** In the app, open **Integrations → Transcript source**, choose **Fathom**, and **Save** with the secret field empty for now. The page then shows your webhook URL, shaped like this. Copy the real one from the page, since it ends in your personal token:

```
https://drowsily-posture-unmoving.ngrok-free.dev/api/webhooks/fathom/<token>
```

☐ **3.** In Fathom, go to **Settings → API Access** and **add a webhook**:

- **Destination:** the URL from the app.
- Turn on **include transcript**.
- **Trigger:** your own recordings.

☐ **4.** Fathom then shows a **webhook secret** starting with `whsec_`. Paste it into the app's Fathom field and **Save**. Without the secret, the app rejects Fathom's calls as unverified.

## Part 6 — Run the test meeting

About 5 minutes.

☐ **1. Schedule a short meeting** on the connected calendar (Zoom Basic or Google Meet) and start it. Wait for Fathom's notetaker to join and admit it. Zoom Basic caps meetings at 40 minutes, which is plenty.

☐ **2. Say commitments clearly, with full names and due dates.** You can play every role yourself. Fathom will credit all the speech to you, so name the owner in each sentence:

> "Priya Shah will send the revised budget to finance by tomorrow."
> "Arjun Mehta, please book the vendor demo by Friday. Okay, I'll book it by Friday."
> "I'll share the meeting notes today."

Using "tomorrow" sets a due date that the day-before cadence acts on right away.

☐ **3. End the meeting.** Fathom sends the transcript a few minutes later, once its processing finishes.

## Part 7 — Check each stage

| Stage | What you should see |
|---|---|
| Transcript arrives | On the dashboard, a "New meeting captured live" pop-up and the meeting with a **Live** badge, without refreshing. The top bar shows **● Live** |
| Extraction | Tasks with owner and due date. The run history on the **AI extraction** page shows your Gemini model, not "fell back" |
| Assignment | Open a task: owner is `you+priya@gmail.com`, matched automatically. If a name wasn't recognised, pick the person by hand in the same window |
| Sheet | New rows in columns A–F: ID, Task, Owner, Due date, Status, Owner email. In an empty sheet the IDs start at **1** |
| Nudge | Within about a minute, a reminder email for the task due tomorrow arrives at your plus-address. **Send test nudge** on a task sends one immediately |
| App → Sheet | Change a task to "In progress" in the app; column E in the Sheet updates |
| Sheet → App | Type `Done` in a row's Status cell (or change its task, owner or due date); the open dashboard updates within seconds and shows a pop-up |

☐ **All seven stages above worked.** If not, record which stage failed, what you saw, and the time, and see below.

## If a step fails

**The URL doesn't load, or shows an ngrok error page**

- The host's app or ngrok isn't running. Tell the host; this isn't an app defect.

**No meeting appears after the Fathom meeting**

- The host can check that the ngrok terminal shows a `POST /api/webhooks/fathom/...` line.
- A **401** means the `whsec_` secret is wrong. A **404** means the token in the URL is wrong; re-copy it from Integrations.
- Nothing at all means Fathom hasn't sent anything yet, or the webhook isn't set up in Fathom.

**You want to test without holding a meeting**

- On **Integrations**, use **Simulate incoming transcript**. It runs the same steps from extraction onward.

**Extraction "fell back" to the built-in model**

- Look at the reason in the run history on the **AI extraction** page.
- **API error 429:** you've hit Gemini's free limit. Wait a minute (per-minute limit) or until tomorrow (daily limit), or switch to a Flash-Lite model, which has higher limits.
- **API error 400 or 404:** the model name is mistyped or no longer offered. Copy the exact name from AI Studio.
- **API error 401 or 403:** the key is wrong or was deleted. Create a new one in AI Studio.

**No email arrives**

- The task needs an owner email from Part 2, and its due date must be tomorrow for the day-before cadence.
- The **Recent nudges** log on Integrations shows the actual SMTP error.

**Sheet edits don't come back to the app**

- In Apps Script, run `testStatusEdit` from the editor and read the execution log.
- **401:** the script is out of date (the signing secret was rotated). Copy it again from Integrations.
- **410:** an old script from before version 0.9.0 is in use. Replace it with the one on Integrations.
- **Integrations → Webhook security** lists recent deliveries and why any were refused.

**Rows don't appear in the Sheet, or "permission denied"**

- Check the Sheet is shared with `vaibhavbhasin95@gmail.com` as **Editor**.
- `invalid_grant`: the host's Google login expired (7 days in "Testing" mode). The host runs `python src\google_oauth_setup.py` again.

> **The test only works while the host's app and ngrok are running.** Automatic nudges and webhooks stop when the host's machine is off.

*Written for Commitment Tracker {{VERSION}} on the developer's shared test server, using free personal accounts only (Fathom, Gmail, Google Sheets, Google AI Studio for Gemini). No paid service is needed.*
