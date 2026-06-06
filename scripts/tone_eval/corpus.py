"""Realistic dictation transcripts for tone post-processing evaluation.

Most samples are >3 words so they route to the LLM path (the ultra-short
bypass in process_with_config fires only at <=3 words). A few intentionally
short slang phrases are included to exercise the lowered bypass.

Each entry:
- id:        stable identifier (used in reports + --samples filter)
- stresses:  tone(s) this sample is designed to exercise
- text:      the raw dictation transcript
- dev_terms: (dev samples) coding terms that MUST survive
- slang:     (professional samples) slang tokens that SHOULD be tidied away
"""

CORPUS = [
    # ----------------------------- DEV -----------------------------
    {
        "id": "dev_01_git_flow",
        "stresses": ["dev"],
        "text": "so um basically i need to git commit the boolean fix on the "
                "feature branch you know and then open a PR for review",
        "dev_terms": ["git", "commit", "boolean", "branch", "PR"],
    },
    {
        "id": "dev_02_refactor",
        "stresses": ["dev"],
        "text": "uh i'm gonna refactor the auth module and like rebase onto main "
                "before i merge because the the diff is getting kinda huge",
        "dev_terms": ["refactor", "auth", "rebase", "main", "merge", "diff"],
    },
    {
        "id": "dev_03_stack_trace",
        "stresses": ["dev"],
        "text": "okay so the null pointer is coming from the cache layer i think "
                "we should add a guard clause and log the request id you know",
        "dev_terms": ["null", "cache", "guard", "clause", "log", "request"],
    },
    {
        "id": "dev_04_api_boolean",
        "stresses": ["dev", "neutral"],
        "text": "um the the api returns a four oh four when the boolean flag is "
                "false so we basically need to check the endpoint config",
        "dev_terms": ["api", "boolean", "flag", "false", "endpoint", "config"],
    },
    # ---------------------------- CASUAL ---------------------------
    {
        "id": "casual_01_texting",
        "stresses": ["casual"],
        "text": "yeah so i'm lowkey gonna head out in like ten minutes you wanna "
                "grab food after or nah im pretty hungry already honestly",
    },
    {
        "id": "casual_02_slang_long",
        "stresses": ["casual", "professional"],
        "text": "oh thats tight bro the new update is actually kinda fire um i was "
                "gonna try it tonight if i dont fall asleep first",
    },
    {
        "id": "casual_03_rambly",
        "stresses": ["casual"],
        "text": "i mean like i dunno i was just thinking we could maybe chill this "
                "weekend you know watch a movie or something nothing crazy",
    },
    {
        "id": "casual_04_relaxed",
        "stresses": ["casual"],
        "text": "haha yeah that meeting was sooo long i almost fell asleep um but "
                "anyway lets catch up later gonna go touch grass real quick",
    },
    # ------------------------- PROFESSIONAL ------------------------
    {
        "id": "prof_01_slang_to_tidy",
        "stresses": ["professional"],
        "text": "oh thats tight bro the quarterly numbers came in way better than we "
                "thought and the client was super stoked about the demo",
        "slang": ["bro", "tight", "stoked", "super"],
    },
    {
        "id": "prof_02_runon",
        "stresses": ["professional"],
        "text": "so um i wanted to follow up on the email i sent yesterday about the "
                "budget because we still havent heard back and the deadlines coming up fast",
    },
    {
        "id": "prof_03_meeting",
        "stresses": ["professional"],
        "text": "yeah basically the team is gonna need a couple more days to wrap up "
                "testing and like we should probably loop in legal before we ship",
        "slang": ["gonna", "like"],
    },
    {
        "id": "prof_04_request",
        "stresses": ["professional"],
        "text": "um could you maybe send over the updated deck when you get a sec i "
                "wanna review it before the the standup tomorrow morning thanks",
        "slang": ["wanna"],
    },
    # --------------------------- NEUTRAL ---------------------------
    # Zero slang, whisper artifacts (to to / the the / a a) — catch over-editing.
    {
        "id": "neutral_01_errand",
        "stresses": ["neutral"],
        "text": "i went to the store this morning and picked up some milk and eggs "
                "and then i stopped by the pharmacy on the way home",
    },
    {
        "id": "neutral_02_plan",
        "stresses": ["neutral"],
        "text": "um we should leave around noon i think so we can beat the traffic "
                "and still have time to grab lunch before the appointment",
    },
    {
        "id": "neutral_03_recap",
        "stresses": ["neutral"],
        "text": "so the the weather was really nice yesterday so we decided to take a "
                "long walk by the river and it was honestly super relaxing",
    },
    {
        "id": "neutral_04_whisper_artifact",
        "stresses": ["neutral"],
        "text": "i think i think we need to to double check the the numbers before we "
                "send it because last time there was a a mistake you know",
    },
    # -------------------- SHORT (lowered-bypass) -------------------
    # 4+ words so they now reach the LLM. The user's flagship slang example.
    {
        "id": "short_01_slang",
        "stresses": ["professional", "casual"],
        "text": "oh thats tight bro nice",
        "slang": ["bro", "tight"],
    },
    {
        "id": "short_02_dev",
        "stresses": ["dev"],
        "text": "um just commit the boolean fix",
        "dev_terms": ["commit", "boolean"],
    },
]


def as_json_serializable():
    return CORPUS
