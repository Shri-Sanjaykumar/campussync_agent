"""Synthetic training mails for the intent model: templates x dates x times x topics,
plus the noise real students add (slang, lowercase, typos). The hand-written evaluation set in
data/eval/mails.jsonl is kept separate so the model is tested on wording it never saw."""
from __future__ import annotations

import random

GREET = ["", "Hi sir,", "Hello ma'am,", "Dear Sir,", "Respected Madam,", "Good morning sir,", "hi mam", "sir,",
         "Ma'am,", "Hello,", "Dear Professor,", "Hey,"]
SIGN = ["", "Thanks", "Regards,\nStudent", "Thank you", "- Arun", "thanks sir", "Kind regards", "Regards"]
TOPICS = ["my project report", "review 2 slides", "the lab record", "my capstone", "CAT-2 marks", "the assignment",
          "attendance issue", "internship NOC", "my research paper", "the hackathon permission", "DA submission",
          "my viva", "the prototype demo", "the survey results", "my thesis draft"]
WHEN = ["tomorrow", "today", "on Monday", "on friday", "this Thursday", "next Tuesday", "on 20th Oct", "on 22/10",
        "day after tomorrow", "this week", "tmrw", "2moro", "on wednesday", ""]
TIMES = ["at 3 pm", "at 10am", "around 4", "at 11:30", "between 2 and 4 pm", "in the morning", "after lunch",
         "in the evening", "at 9", "", "", "by 5"]
DUR = ["", "", "for 10 minutes", "for half an hour", "for 15 mins", "for an hour", "20 min"]
ORD = ["first", "second", "third"]

TEMPLATES = {
    "meeting_request": [
        "Can we meet {when} {time} to discuss {topic}?", "I would like to meet you {when} {time} regarding {topic}.",
        "Could I come to your cabin {when} {time}?", "Is it possible to meet {when} {time} {dur}?",
        "Please meet me {when} {time} regarding {topic}.", "can i meet u {when} {time} abt {topic}",
        "Need {dur} of your time {when} for {topic}.", "Would you be available {when} {time} for a quick discussion on {topic}?",
        "Requesting an appointment {when} {time} to discuss {topic}.", "{when} {time} ok for a meeting about {topic}?",
        "I need to show you {topic}, can I drop by {when} {time}?", "Kindly give me a slot {when} {time} to clear doubts on {topic}.",
        "Are you free {when} {time}? I want to discuss {topic}.", "Could you spare {dur} {when} for {topic}?",
        "Our team wants to meet you {when} {time} about {topic}.", "Can I see you {when} {time} regarding {topic}?"],
    "availability_query": [
        "When are you free {when} for {topic}?", "When would you be available {when}?",
        "Could you let me know your free slots {when}?", "What time works for you {when} to discuss {topic}?",
        "Please share your availability {when} {dur}.", "when r u free {when}", "Which slot is convenient for you {when}?",
        "When can I meet you {when} regarding {topic}?", "Let me know when you have some free time {when}.",
        "At what time can we meet {when}?", "What are your free timings {when}?", "When is a good time to discuss {topic}?"],
    "reschedule": [
        "Can we reschedule our meeting to {when} {time}?", "I need to postpone our meeting, can we do {when} {time} instead?",
        "Could we move the {topic} discussion to {when} {time}?", "Something came up, can we shift our meeting to {when}?",
        "Is it possible to change the time of our meeting to {when} {time}?", "pls shift our mtg to {when} {time}",
        "Can we push our meeting to {when}?", "I have a clash with our meeting, could we do it {when} {time} instead?",
        "Can we prepone the meeting to {when} {time}?", "Would it be okay to meet {when} {time} instead of the earlier time?",
        "Can our {topic} meeting be moved to {when}?", "I can't make the earlier time, how about {when} {time}?"],
    "cancel": [
        "I won't be able to make it to our meeting {when}, please cancel it.", "Please cancel the meeting about {topic}.",
        "Sorry, I can't make it {when}. Kindly cancel our meeting.", "I need to cancel our appointment {when}.",
        "cant come {when} sir, pls cancel", "Our meeting {when} is no longer needed, please call it off.",
        "Due to an emergency I have to cancel the meeting {when}.", "I'm unable to attend the meeting {when}, sorry.",
        "Please drop our meeting {when}, the issue is sorted.", "We don't need the {topic} meeting anymore, please cancel."],
    "option_choice": [
        "Option {n} works for me.", "I'll take option {n}.", "The {ord} option is fine.", "option {n} pls",
        "Option {n} is good, thank you.", "{ord} one works for me sir", "Let's go with option {n}.", "Slot {n} is perfect.",
        "I prefer option {n}.", "Option {n} please."],
    "leave_request": [
        "I would like to apply for leave {when} due to fever.", "Kindly grant me leave {when}, I have a family function.",
        "My son will be absent {when} as he is unwell.", "Please sanction leave for my daughter {when}.",
        "I need leave {when} for my sister's wedding.", "requesting sick leave {when} sir",
        "Respected sir, please grant my ward leave {when}.", "I am down with fever, need leave {when}.",
        "She will be on leave {when} due to a medical emergency.", "leave application for {when}, going home"],
    "other": [
        "Thank you for the feedback on {topic}.", "PFA {topic}.", "I have submitted {topic} on the portal.",
        "Noted, thank you.", "ok sir", "Thanks a lot ma'am!", "Please find attached {topic} for your reference.",
        "I have completed {topic}.", "Could you share the slides for {topic}?", "Will do, thanks.",
        "Is {topic} due this Friday?", "What is the deadline for {topic}?", "Received, thank you.", "Happy Diwali sir!",
        "Sorry for the late submission of {topic}.", "Can you check {topic} and send comments?"],
}
SLANG = [("you", "u"), ("please", "pls"), ("tomorrow", "tmrw"), ("meeting", "mtg"), ("are", "r"), ("about", "abt"),
         ("minutes", "mins"), ("thanks", "thx")]


def _noise(text: str, rnd: random.Random) -> str:
    if rnd.random() < 0.35:
        text = text.lower()
    if rnd.random() < 0.3:
        for a, b in SLANG:
            if rnd.random() < 0.5:
                text = text.replace(f" {a} ", f" {b} ")
    if rnd.random() < 0.2:
        words = text.split()
        i = rnd.randrange(len(words))
        w = words[i]
        if len(w) > 3:
            j = rnd.randrange(len(w) - 1)
            words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2:]
        text = " ".join(words)
    if rnd.random() < 0.2:
        text = text.replace("?", "").replace(",", "")
    return text


def generate(n_per_class: int = 220, seed: int = 0) -> tuple[list[str], list[str]]:
    rnd = random.Random(seed)
    X, y = [], []
    for label, temps in TEMPLATES.items():
        for _ in range(n_per_class):
            t = rnd.choice(temps).format(when=rnd.choice(WHEN), time=rnd.choice(TIMES), topic=rnd.choice(TOPICS),
                                         dur=rnd.choice(DUR), n=rnd.randint(1, 3), ord=rnd.choice(ORD))
            body = " ".join(x for x in (rnd.choice(GREET), t, rnd.choice(SIGN)) if x)
            X.append(_noise(" ".join(body.split()), rnd))
            y.append(label)
    return X, y
