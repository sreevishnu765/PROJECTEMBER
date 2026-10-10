// A reference of everything Ember responds to. Phrases here are taken from the actual matchers in
// ember_voice_text.py, ember_intent.py and ember_core.py's tool registry. Wording is flexible for
// most of them (they are patterns, not exact strings); the examples are the shapes that are known to work.
// When a command is added or changed in the backend, update this file too.

interface Entry {
  say: string;
  does: string;
  note?: string;
}
interface Section {
  title: string;
  intro?: string;
  entries: Entry[];
}

const SECTIONS: Section[] = [
  {
    title: "Before you start (voice)",
    intro:
      "Voice commands only work while the microphone is switched on in the app (the mic button), and only when Ember is addressed. Typing never needs a wake word.",
    entries: [
      { say: "\u201CHey Ember\u201D", does: "Wakes her. Works anywhere in what you say. Say nothing else and she answers \u201CSir?\u201D and waits." },
      { say: "\u201CHey Ember, what\u2019s the time?\u201D", does: "Name and command in one breath." },
      { say: "\u201CEmber\u2026\u201D or \u201CWake up\u2026\u201D", does: "Also wake her, but only as the very first words of what you say." },
      { say: "Keep talking after a reply", does: "For about 7 seconds after she finishes speaking, no wake word is needed." },
      { say: "\u201CEmber, you there?\u201D", does: "Presence check. Turns voice mode on and opens the HUD.", note: "Also: \u201Care you there\u201D, \u201Ccan you hear me\u201D." },
    ],
  },
  {
    title: "Voice mode (hands-free)",
    intro: "Once it is on there is no wake word and no timeout until you turn it off.",
    entries: [
      { say: "\u201CHey Ember, voice mode\u201D", does: "Turns it on. You have to wake her first; a bare \u201Cvoice mode\u201D is ignored.", note: "Also \u201Chey Ember, let\u2019s talk\u201D. The voice button in the app does the same thing." },
      { say: "\u201CStop listening\u201D, \u201Cgo to sleep\u201D, \u201Cexit voice mode\u201D, \u201Cwe\u2019re done\u201D", does: "Turns it off. These work without the wake word while it is on." },
    ],
  },
  {
    title: "Stopping and ending",
    entries: [
      { say: "\u201CStop\u201D, \u201Cquiet\u201D, \u201Cshut up\u201D, \u201Cenough\u201D, \u201Cnever mind\u201D, \u201Chold on\u201D, \u201Cwait\u201D", does: "While she is speaking or working: cuts her off and cancels the task. No wake word needed.", note: "Must be the whole phrase. \u201CStop the music\u201D is treated as a normal request about the music. When nothing is running it does nothing." },
      { say: "\u201CThank you\u201D, \u201Cthat\u2019s all\u201D", does: "Ends the conversation: she goes quiet until the wake word.", note: "Normal wake-word use only. In voice mode it is just an ordinary message." },
      { say: "\u201CYes\u201D / \u201CNo\u201D", does: "Answers an approval she asked out loud, with no wake word.", note: "Deleting memory and running scripts are approved on screen only." },
    ],
  },
  {
    title: "Reminders and timers",
    intro: "They appear in the chat, raise a desktop notification and are spoken aloud when they fire. Voice does not need to be switched on.",
    entries: [
      { say: "\u201CRemind me to submit the report tomorrow at 6 PM\u201D", does: "One-off reminder. She says \u201CReminder to submit the report, sir.\u201D when it fires." },
      { say: "\u201CRemind me in 3 hours and 40 minutes to\u2026\u201D", does: "Other time shapes: in N minutes/hours (also \u201Can hour and a half\u201D, \u201Chalf an hour\u201D), at 5pm, tonight, this evening, noon, a weekday name." },
      { say: "\u201CRemind me every Sunday to\u2026\u201D", does: "Repeating reminder: every day, every week, or a named weekday." },
      { say: "\u201CSet a timer for 10 minutes\u201D", does: "Countdown. \u201CYour 10 minute timer is up, sir.\u201D", note: "Also \u201Cstart a 5 minute timer for the pasta\u201D and \u201Cset a 30 second timer\u201D." },
      { say: "\u201CShow active reminders\u201D", does: "Lists active reminders and timers with their IDs.", note: "Also \u201Cwhat are my reminders\u201D, \u201Cdo I have any reminders\u201D, or just \u201Creminders\u201D." },
      { say: "\u201CCancel the wake up reminder\u201D, \u201Ccancel wake up\u201D", does: "Cancels the one whose words match." },
      { say: "\u201CCancel the 30 minute reminder\u201D, \u201Ccancel the 5pm one\u201D", does: "Finds it by how long you set it for, or by its time." },
      { say: "\u201CCancel <id>\u201D, \u201Cstop the pasta timer\u201D, \u201Ccancel it\u201D", does: "By ID, by timer label, or \u201Cit\u201D for the one you just set or listed (or the only one)." },
      { say: "If more than one matches", does: "She lists them and asks. Answer \u201Cthe second one\u201D, \u201C2\u201D, an ID, or \u201Call of them\u201D.", note: "Identical duplicates are cancelled together." },
      { say: "\u201CDelete all reminders\u201D, \u201Cclear my timers\u201D", does: "Cancels everything of that kind. Asks first." },
      { say: "If nothing matches", does: "She says nothing was cancelled and shows what is active; she never claims a cancel that didn\u2019t happen." },
    ],
  },
  {
    title: "Files",
    intro:
      "A file request must make clear it is about a file: say \u201Cfile\u201D or \u201Cfolder\u201D, give a name with an extension, or name a type or well-known folder. That is what stops \u201Cmove the meeting to Friday\u201D being treated as a file move.",
    entries: [
      { say: "\u201CMove the budget file to Documents\u201D", does: "Moves a file, found by name, into a folder." },
      { say: "\u201CRename the budget file to final budget\u201D", does: "Renames a file." },
      { say: "\u201CDelete the old export file\u201D", does: "Moves it to Ember\u2019s trash after you confirm." },
      { say: "\u201CUndo that\u201D", does: "Reverses the last move, rename or delete." },
      { say: "\u201COpen notes.txt\u201D, \u201Copen Spotify\u201D", does: "Opens a file or folder in its default app, or launches an application." },
      { say: "\u201CShow budget.xlsx in explorer\u201D", does: "Reveals the file in File Explorer." },
      { say: "\u201CList files in <folder>\u201D, \u201Cread file at <path>\u201D", does: "Browse or read inside folders you have allowed." },
      { say: "\u201CAllow access to <folder>\u201D", does: "Adds a folder to the allowed list. Asks first." },
      { say: "\u201CRun script at <path>\u201D", does: "Runs an allowed script. Always asks first." },
      { say: "\u201CDownload <Google Drive link>\u201D", does: "Downloads a Drive file or folder." },
    ],
  },
  {
    title: "Memory",
    entries: [
      { say: "\u201CRemember that I prefer tea\u201D", does: "Saves a fact. Ending with \u201C\u2026, remember that\u201D works too." },
      { say: "\u201CWhat do you remember about my city?\u201D", does: "Reads back what she has stored." },
      { say: "\u201CSearch memory for where I live\u201D", does: "Looks through memory by meaning, not exact wording." },
      { say: "\u201CUpdate my city to Pune\u201D", does: "Replaces an old fact instead of adding a conflicting one." },
      { say: "\u201CForget that I\u2019m watching a show\u201D", does: "Removes matching memories and tells you which. Asks first." },
      { say: "\u201CClear my memory\u201D", does: "Wipes everything. Always asks first." },
      { say: "\u201CWhere do I live?\u201D", does: "Checks memory first. If it is not stored she says so rather than guessing." },
    ],
  },
  {
    title: "Search and research",
    entries: [
      { say: "\u201CSearch for\u2026\u201D, \u201Clook up\u2026\u201D, \u201Cgoogle\u2026\u201D", does: "Forces a live web search." },
      { say: "\u201CFind out when the next race starts\u201D", does: "Same: \u201Cfind out \u2026\u201D and \u201Cfind the \u2026\u201D go to the web." },
      { say: "Current-events wording: \u201Ctoday\u201D, \u201Cthis evening\u201D, \u201Ctomorrow\u201D, \u201Clatest\u201D, \u201Cwho won\u2026\u201D, scores, prices, \u201Cwhat time is the match/race\u2026\u201D", does: "Searched automatically. The reply is tagged \u201Cgrounded\u201D when live sources were used." },
      { say: "\u201CAre you sure?\u201D, \u201Crecheck that\u201D, \u201Cverify\u201D", does: "Re-checks her last claim against fresh sources instead of repeating it." },
    ],
  },
  {
    title: "Calendar, music, screen, export",
    entries: [
      { say: "\u201CWhat\u2019s on my calendar today?\u201D", does: "Reads your Google Calendar. \u201Ctomorrow\u201D or a weekday work too." },
      { say: "\u201CSchedule dentist for tomorrow at 4pm\u201D", does: "Adds an event." },
      { say: "\u201C4pm, Ember\u201D (right after)", does: "Corrects the time of the event just created." },
      { say: "\u201CReschedule the team meeting to Wednesday\u201D", does: "Moves an event, found by name." },
      { say: "\u201CCancel the dentist meeting\u201D, \u201Ccancel that\u201D", does: "Deletes an event. Asks first." },
      { say: "\u201CPlay <song> on Spotify\u201D", does: "Finds the track and starts it in the Spotify app." },
      { say: "\u201CWhat\u2019s on my screen?\u201D", does: "Takes a screenshot and describes it." },
      { say: "\u201CExport that as a PDF\u201D", does: "Saves her last reply as a PDF." },
    ],
  },
  {
    title: "Attachments in chat",
    intro: "Use the paperclip to attach images, PDFs or text files.",
    entries: [
      { say: "Attach a file and ask about it", does: "She reads it (images are described by a vision model) and answers from that." },
      { say: "\u201CI think it\u2019s actually\u2026\u201D, \u201Clook again\u201D, \u201Care you sure?\u201D", does: "Disagreeing makes her re-examine the image with your suggestion in mind and weigh it, rather than defend the first reading." },
      { say: "Follow-up questions", does: "Attachments stay available for about five messages, and earlier ones are kept when you add new ones." },
    ],
  },
  {
    title: "Status and housekeeping",
    entries: [
      { say: "\u201CWhat time is it?\u201D, \u201CWhat\u2019s the date?\u201D, \u201CWhat day is it?\u201D", does: "Answered instantly from the clock, with no search and no model." },
      { say: "\u201CEmber, status\u201D, \u201Care your systems online?\u201D", does: "Cloud, fallback providers, memory and reminder counts." },
      { say: "\u201CDiagnose yourself\u201D, \u201Cdiagnose the cloud\u201D", does: "Self-check with remaining quota." },
      { say: "\u201CClear context\u201D, \u201Cnew chat\u201D, \u201Clet\u2019s start fresh\u201D", does: "Forgets this conversation only. Long-term memory is untouched." },
    ],
  },
  {
    title: "Teaching her your speech",
    intro: "For names and words she keeps mishearing. Stored in data/stt_vocabulary.txt, which you can also edit by hand.",
    entries: [
      { say: "\u201CAdd <word> to your vocabulary\u201D", does: "She listens for that word and fixes near-miss spellings of it." },
      { say: "\u201CWhen you hear <what she wrote>, I mean <what you said>\u201D", does: "Fixes one specific, repeated mishearing." },
    ],
  },
];

export function GuidePanel() {
  return (
    <div className="space-y-6">
      <p className="text-[12.5px] leading-relaxed text-textSecondary">
        What Ember understands. Phrases are patterns, so natural variations often work; these are the shapes
        that are tested. Anything that needs approval (deleting, running scripts, wiping memory) asks first.
      </p>
      {SECTIONS.map((section) => (
        <section key={section.title}>
          <h3 className="mb-1 text-[11.5px] font-medium uppercase tracking-wide text-textMuted">{section.title}</h3>
          {section.intro && <p className="mb-2 text-[12px] leading-relaxed text-textMuted">{section.intro}</p>}
          <div className="divide-y divide-border rounded-lg border border-border">
            {section.entries.map((entry) => (
              <div key={entry.say} className="px-3 py-2">
                <div className="text-[13px] text-textPrimary">{entry.say}</div>
                <div className="mt-0.5 text-[12.5px] text-textSecondary">{entry.does}</div>
                {entry.note && <div className="mt-0.5 text-[11.5px] text-textMuted">{entry.note}</div>}
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
