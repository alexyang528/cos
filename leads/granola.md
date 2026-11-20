# Granola lead

## Owns
Meetings {me} attended that Granola took notes for. Find the action items that are {me}'s and queue each
one as a task, so nothing {me} took on in a meeting gets lost.

## How to read the source
- mcp__claude_ai_Granola__get_meetings(meeting_ids=[id]) for the AI summary, {me}'s private notes,
  attendees and the note URL. The route's event has the meeting id.
- The summary condenses and sometimes misattributes. Confirm every candidate with
  mcp__claude_ai_Granola__search_transcripts(meeting_ids=[id], keywords=[...]): who took it on, what
  exactly, and any date. Read the transcript (get_meeting_transcript) when the excerpts don't settle it.
- {me} is {email}. When {me} created the note, {me}'s speech is usually the Microphone
  source. Next Steps / Action Items lines such as "{me} to ..." are a lead, not proof.
- Use Slack, Linear, Notion or GitHub reads to check whether an item is already done, and for links that
  make the task easy to act on (the thread, ticket or PR it is about).

## What counts as {me}'s action item
- {me} said they would do it ("I'll send...", "let me check...", "I can take that"), someone asked {me} and
  {me} agreed, or the notes assign it to {me} and the transcript doesn't contradict that.
- Not {me}'s: things another person took on, decisions with no follow-up, ideas nobody took ("we should
  think about..."), and things finished during the meeting.
- An item that clearly needs doing in {me}'s area but nobody took: queue it as a task and say in the body
  that nobody was assigned.

## Before queuing
- Drop an item that is already done, with the evidence in the reason (e.g. {me}'s Slack message to Priya
  after the meeting, the Linear issue {me} filed).
- Recurring meetings repeat follow-ups. If one of your open items already covers it, don't queue it again.

## What good output looks like
- One task item per action item. A meeting with several gives several items, all with the meeting's route.
- title: "<meeting title>: <what to do>", e.g. "Priya / {me}: send Priya the onboarding page outline".
- summary: who asked for it, what exactly, and by when only if a date was said.
- body: the action as an instruction to {me} first, then the context needed to do it without reopening
  the meeting: what was discussed (in your words, never quoted), links, who is waiting, the meeting date.
- When the action item is open-ended work (write the outline, dig into why X happened, build Y), draft an
  agent prompt with the meeting context and links in it. A quick chore (send a link, book a meeting) gets no
  prompt.
- priority 0 only when it is due today or tomorrow or someone is blocked on it now; otherwise 1.
- url: the Granola note URL.
- A meeting with no action items for {me} goes in "dropped" ("no action items for {me}").
