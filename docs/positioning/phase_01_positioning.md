# Deft — Positioning Document
**Phase 01 of 24. The thesis the rest of the company defends.**
Date: 2026-04-25
Status: Locked. Changes require a counter-thesis with evidence.

---

## 0. Read me first

This document is the source of truth for Deft's positioning. The website copy, the in-product voice, the pricing page, the error states, the founder posts, and every piece of marketing collateral derive from it. If a line of copy contradicts this document, the copy is wrong.

It contains seven sections:

1. The enemy — what we are fighting against, named precisely
2. The thesis — 1-line, 3-line, and 30-line versions, with defended choices and rejected drafts
3. The category decision — what we are vs. what they are
4. The comparison table — head-to-head against Lovable, Bolt, v0, Replit Agent, Cursor, and Devin
5. The brand voice rules — banned words and tonal directives
6. Three voice samples — hero, error message, pricing FAQ
7. What this commits us to — the obligations the thesis creates

---

## 1. The enemy

The enemy is not a competitor. The enemy is **debug hell** — the broken-loop screaming match where a person types a prompt, gets back code that almost works, runs it, watches it fail, screenshots the failure, pastes the screenshot back, gets a new version that breaks something different, and repeats this for two hours until they give up or accept a half-working app.

This is the lived experience of vibe coders today. It is the dirty secret of every AI coding tool on the market. The marketing pages do not mention it. The hero copy does not mention it. The pricing pages do not mention it. The user knows the truth anyway, because the user has lived it.

Every competitor has decided that the polite move is to pretend debug hell does not exist. Lovable says "Build something Lovable." Bolt says "What will you build today?" v0 says "What do you want to create?" Replit says "What will you build?" Every one of them ends the headline at the moment of generation, as if generation were the destination. It is not. Generation is the start of debug hell.

Deft's job is to name this and end it.

Why we can end it: every other AI coding tool generates code and stops. Deft generates code and then runs it in a real browser, watches its own output, and fixes its own mistakes before the user sees them. The mechanism is having a real computer. The result is not having to debug.

The enemy has a face. It looks like:

- A user yelling at an AI for the third hour
- A "fixed" version that breaks the previous fix
- A screenshot pasted back as evidence that the AI did not look at its own work
- A console error the agent never saw because the agent never opened the console

We exist to delete this experience. Everything below follows from that.

---

## 2. The thesis

### 2.1 One-line thesis

> **The AI developer that doesn't leave you debugging.**

#### Why this line

It does five things at once:

1. **Names the role.** "AI developer" — Deft is the developer. The user is the product manager / creative. This is the inversion vibe coders want and no other tool says out loud.
2. **Names the enemy.** "debugging" — the word every competitor avoids. We use it because the user lives it.
3. **Implies the competitive frame.** "doesn't leave you" — implies that the others do leave you. They do. We don't have to say their names.
4. **Calm declarative.** No exclamation, no superlative, no number. Voice rule satisfied.
5. **Survives the read-aloud test.** A vibe coder reads it and recognizes their own pain. A skeptic reads it and immediately wants to know how. That is the right reaction.

#### Rejected one-line drafts and why

| Draft | Reason rejected |
|---|---|
| "Build something that actually works." | "Build" is the saturated competitor verb. Lovable, Bolt, v0, Replit, Devin, Claude Code all use it. Forbidden. |
| "Deft ships working software. Not code that might work." | "Ships" is in the banned list. "Working software" is good but the line negates itself, which weakens the claim. |
| "The AI engineer that tests before you see it." | "Tests before you see it" is mechanism. The headline should be result, not mechanism. The mechanism belongs in the subhead. |
| "Code that runs. Without you fixing it." | Two sentences. Loses the rhythm. "Without you fixing it" is a negation of a negation and reads tortured. |
| "The first AI developer that verifies before it ships." | "First" is a brittle claim — falsifiable in a tweet. "Verifies" is engineer-speak; vibe coders don't say it. |
| "AI development that ends with working software." | Soft. Doesn't name the enemy. Reads like a B2B SaaS subhead from 2019. |
| "Stop debugging AI." | Imperative voice. Closer than most, but reads like a taunt. We are not taunting — we are confident and quiet. |
| "Deft writes code that runs." | Strong, but "writes code" is the saturated frame. We do more than write — we run, watch, fix. |
| "The AI that actually codes." | The user has used this informally, but "actually" is defensive. Confident voice does not say "actually." |
| "Working software, on the first try." | Quantitative claim ("first try") is a stat trap. Will be screenshotted by someone who got it wrong on the second try. |

#### What the line does not promise

It does not promise zero bugs. It does not promise the AI is always right. It promises that **debugging is not the user's job**. If the AI is wrong, the AI fixes it before the user sees it. That is the contract.

### 2.2 Three-line thesis

> **Deft is the AI developer that doesn't leave you debugging.**
> **It runs your app in a real browser, watches its own output, and fixes its own mistakes — before you see them.**
> **You describe what you want. Deft delivers software that runs.**

#### Why this version

- Line 1: the result. The promise. The thing the user came for.
- Line 2: the mechanism. The reason it is true. Names the three concrete agentic actions: runs, watches, fixes. Each is a verb the AI does, not a verb the user does.
- Line 3: the contract. The exchange. You describe; Deft delivers. The closing word is "runs" — the most loaded word in software. Software that runs is the only kind worth delivering.

#### Phrase-level defense

- "real browser" — the differentiator. Not a sandbox, not a simulator. Real Chrome with real DOM and real network requests. Concrete and verifiable.
- "watches its own output" — the inversion of the broken-loop screaming match. The AI is the one looking. Not the user.
- "fixes its own mistakes" — admits mistakes happen. Confident voice does not pretend infallibility. It claims responsibility.
- "before you see them" — the entire user-experience promise in four words. The user does not see the failure mode. The user sees working software.
- "describe / delivers" — the contract. Two verbs. One for the user, one for Deft.

### 2.3 Thirty-line thesis (the manifesto)

> Every AI coding tool ends its job at the wrong moment.
>
> They write the code, hand it to you, and walk away. What you got is a draft. What you needed was working software. The gap between those two things is debug hell — the screaming match between you and an AI that never bothered to run what it wrote.
>
> This is the dirty secret of vibe coding. The product demo always works. Your app does not. You re-prompt. You paste screenshots. You explain to the AI what its own console says. You do this for two hours. Sometimes you give up. Often you settle for half.
>
> The reason this happens is not that the models are bad. The models are excellent. The reason is that the models cannot see what they made. They generate code into a void and trust that you will tell them what went wrong. You are the eyes. You are the test runner. You are the QA. You are the bug report. The AI did the easy part. You did everything else.
>
> Deft fixes this by giving the AI what every other tool refuses to give it: a real computer.
>
> Deft writes the code, then runs the app in a real browser. It watches the page render. It clicks the buttons. It reads the console. It catches the error before you do. When something is broken, Deft is the one who finds it, and Deft is the one who fixes it. You see the second version, not the first. You see software that runs.
>
> This is not a sandbox demo. It is not a "preview." It is the same browser, the same network, the same DOM the user will eventually use. The AI's eyes are on the same screen yours would have been on, and its hands are on the same keyboard. The work that used to be your job is now its job.
>
> Deft is for the people building real things from a prompt — the founders, the designers, the operators, the indie makers, the engineers who would rather direct than type. You stay the product person. Deft is the developer. The contract is simple: you describe what you want, Deft delivers software that works.
>
> We do not promise the AI is never wrong. We promise the AI's mistakes do not become your mistakes. The fix happens before the handoff. The version you see is the version that runs.
>
> That is the entire bet.

#### Why this version

This is the version that lives on `/manifesto` and gets quoted in launch posts. It is structured as a six-beat argument:

1. **Diagnosis** (lines 1–4): every AI coding tool ends at the wrong moment.
2. **Lived experience** (lines 5–7): you, screaming at the AI, doing its job.
3. **Root cause** (lines 8–11): the AI cannot see what it made.
4. **Fix** (lines 12–15): give it a real computer.
5. **Proof of concreteness** (lines 16–18): real browser, real DOM, real keyboard.
6. **Contract and bet** (lines 19–22): the exchange and the line we will be judged on.

It deliberately does not include numbers, customer logos, or feature lists. Those belong on landing-page modules below the manifesto, not inside it.

---

## 3. The category decision

### 3.1 We do not claim a new category.

Claiming a new category — "Verified AI Development," "AI QA Engineering," "Self-Testing Code Generation" — sounds clever in a strategy document and reads as marketing slop on a page. Vibe coders do not buy categories. They buy outcomes. We compete inside the existing category ("AI coding tools" / "AI app builders") and we win on the dimension nobody else owns: **the AI does its own debugging**.

### 3.2 The framing we do use

Two camps, named in plain English:

- **Code generators**: tools that write code and stop. Lovable, Bolt, v0, Replit Agent, Cursor, Claude Code. They hand you a draft.
- **Software deliverers**: tools that write code, run it, watch it, fix it, and hand you the version that works. Deft. Devin's "Visual QA" feature gestures at this but does not lead with it.

This frame is the lens for every comparison, every blog post, and every objection-handling FAQ. We never call ourselves a category by a new name. We point at the others and say: *they generate. We deliver.*

### 3.3 Why this is the right play

- Naming a new category requires the world to learn a new word. We do not have the budget. We do have a sharper claim than the incumbents.
- The vibe-coder audience is not category-curious. They are pain-curious. Lead with their pain.
- "The AI developer that doesn't leave you debugging" works without any category at all. The line is self-explanatory. That is the test.

---

## 4. The comparison table

For the website. Six columns, six rows. Every cell is defensible.

| Capability | Lovable / Bolt / v0 / Replit | Cursor | Devin | **Deft** |
|---|---|---|---|---|
| **What you describe** | A prompt | A task in your codebase | A ticket | A prompt |
| **What you get back** | Code in a preview | Code in your editor | A pull request | Software that runs |
| **Runs the app in a real browser** | No | No | Yes (buried as "Visual QA") | **Yes — every change, every time, surfaced as the headline behavior** |
| **Catches its own bugs before handoff** | No | No (you review) | Partial | **Yes — the version you see is the one that passed** |
| **Default failure mode** | You debug | You debug | PR has bugs you find in review | Deft fixes and re-runs before showing you |
| **You only pay for working software** | No (you pay per prompt) | No (you pay per token) | No (you pay per session) | **Yes — credits charge against successful work, with caps you set** |

#### Defended choices

- **No exclamation marks, no checkmarks, no green-yes / red-no theater.** The table is a statement of fact, not a scoreboard.
- **"Software that runs" is the only cell containing a finished noun.** Every other "What you get back" cell is a draft.
- **Devin gets credit for visual QA.** Pretending Devin does not have a related capability would make us look threatened. Naming it and noting it is buried demonstrates we have actually read their site. Credibility from honesty.
- **Pricing row is included** because it is positioning, not just pricing. "You only pay for working software" is a thesis claim — the AI is responsible for delivering, so we are responsible for not charging for failure.

---

## 5. The brand voice rules

### 5.1 Tonal directives

> **Confident. Quiet. Competent. Never salesy. Never hype-y. Never cute.**

Operationalized:

- **Lead with the answer, then the explanation.** No throat-clearing.
- **Declarative sentences. Few questions.** Questions in headlines are the saturated competitor move.
- **No second person imperative as headline.** "Try Deft." "Get started." "Make your idea real." None of these. The user finds us; we do not order them.
- **No counters, no stats in the hero.** Numbers that change weekly are noise. Reserve numbers for the case-studies section, where they earn their place.
- **No promises about the future.** No "the future of." No "next-gen." No "10x." The product works in the present tense.
- **Admit what is hard.** "We do not promise the AI is never wrong" is on-brand. It earns the line that follows.

### 5.2 Banned words and phrases

These are forbidden in any user-facing surface — landing page, in-product, error states, pricing copy, social posts, founder posts.

**Verbs:**
build (as hero verb), ship, supercharge, empower, unlock, transform, accelerate, revolutionize, reimagine, elevate.

**Adjectives:**
magical, magic, amazing, stunning, revolutionary, seamless, effortless, beautiful (for our own product), powerful, intelligent, smart (as self-description), next-gen, cutting-edge, state-of-the-art, best-in-class, world-class, game-changing, mission-critical.

**Phrases:**
"let's build X together", "start your journey", "the future of X", "the X you've been waiting for", "AI-powered", "AI-native", "vibe coding" (as Deft's own self-description; it is a user description, not a product description), "build something [X]", "what will you build", "what do you want to create", "turn ideas into apps", "describe what you want and we'll build it", "10x faster", "98% fewer errors", any "X faster" / "X cheaper" / "X less" stat in the hero, "HARD CREDIT CEILING," any all-caps badge, any "trusted by" without named logos.

**Punctuation:**
Exclamation points. Em-dashes are permitted. Emojis are forbidden everywhere.

### 5.3 Allowed but careful

- "deliver" — allowed, but only in the contract sense ("Deft delivers software that runs"). Not "deliver value."
- "works" / "working" — allowed and encouraged. The most loaded word we have.
- "real" — allowed (real browser, real DOM, real network). Earned by the mechanism. Do not stretch to "real AI" or "really powerful."
- "debug" / "debugging" — allowed and encouraged. Our enemy. Use it more than they do.
- "developer" — allowed only when describing Deft itself ("the AI developer"). Not for the user.
- "you" — allowed. Required, even. Just never in imperative-voice headlines.

---

## 6. Three voice samples

### 6.1 Hero (landing page top fold)

> **The AI developer that doesn't leave you debugging.**
> **Deft runs your app in a real browser, watches its own output, and fixes its own mistakes — before you see them.**
>
> [ Describe what you want to build ] [ Start ]
>
> *No exclamation. No counter. No "trusted by" row above the fold. No badge. No gradient sphere. Just the two lines and the prompt input.*

### 6.2 Error message (in-product, when the agent hits a real error it cannot recover from)

> **A step did not pass and Deft could not recover automatically.**
>
> Deft tried three approaches and the app still does not run cleanly. The most likely cause is a Supabase row-level-security rule the agent cannot read. We have paused work and saved a diff so nothing is lost.
>
> *Two options:*
>
> [ Show me the diff ] [ Have Deft try a different approach ]
>
> ---
> *What we did **not** write:*
>
> ~~"Oops! Something went wrong."~~
> ~~"Don't worry, we're on it!"~~
> ~~"We're sorry for the inconvenience."~~
> ~~"Please try again later."~~
>
> Deft does not apologize, panic, or make small talk. It reports what happened, what it tried, and what the user can do next. Calm, agentic, specific. The user is not a customer being soothed. The user is a collaborator being briefed.

### 6.3 Pricing FAQ (one entry)

> **Q: What if Deft makes a mistake and burns my credits?**
>
> A: Deft only charges credits for successful work. If a step fails and Deft cannot recover, the credits for that step are not deducted. If a finished feature does not run when handed off, you can flag it and the credits are returned. Deft is responsible for delivering software that works; charging for software that does not work would contradict that. You set a monthly cap when you sign up and Deft will not exceed it without asking you first.
>
> ---
> *What we did **not** write:*
>
> ~~"HARD CREDIT CEILING — Deft will NEVER exceed your spend."~~
> ~~"Don't worry, we protect you from runaway costs."~~
> ~~"You're always in control of your spend!"~~
>
> The first version is defensive anxiety language in all caps. It signals that the company expected to be accused of stealing money. The rewrite is calm and outcome-framed: you only pay for working software, and the cap is yours to set. Same policy. Different posture. The posture is the brand.

---

## 7. What this thesis commits us to

A position is only useful if it constrains what we will not do. This one constrains us as follows:

1. **Every release must close more of the debug-hell loop, or it does not go out.** Speed improvements that do not reduce debugging are at best neutral, at worst off-strategy.
2. **The browser-self-test capability must work, visibly, on the demo.** If the user does not see the agent watching its own work, the headline is not earned. The demo must show the agent catching and fixing its own bug in a real browser.
3. **We cannot claim "zero bugs" or "100% working software."** The thesis is "the agent's mistakes do not become your mistakes." Not "no mistakes." Confident voice respects the listener's intelligence.
4. **Pricing must reflect outcome accountability.** Credits charge against successful work. Failed steps that the agent itself rolls back are not billed. This is on the pricing page, not in a footnote.
5. **No competitor name appears in our hero.** The framing ("they generate, we deliver") is general. We name competitors only on a comparison page, where we credit them for what they do well and explain the dimension we win on.
6. **Founder posts and launch copy follow the manifesto.** The opening of any launch post is a variation on "every AI coding tool ends at the wrong moment." That is the wedge.
7. **The product UI must reinforce the thesis.** The status indicator must show the agent watching the browser. The error states must say what Deft tried, not "oops." The history view must let the user replay what the agent saw.

If a future decision contradicts any of the seven, the decision is wrong, or the thesis needs to change. The thesis does not change without a new positioning document.

---

## 8. Appendix — the rejected positioning frames

Recorded for the next person who is tempted to revisit them.

| Rejected frame | Why rejected |
|---|---|
| **"Lovable for serious builders"** | Adjacent positioning. Reduces us to a Lovable variant. Lovable's audience does not want serious. Serious builders are not on Lovable. |
| **"The Cursor for non-engineers"** | Cursor's audience is engineers. The line is a contradiction. |
| **"AI QA Engineer"** | Sounds like a job title we made up. Vibe coders do not hire QA engineers and would not buy one. |
| **"Verified AI Development"** | Category-creation move. Requires the world to learn a phrase. We do not have the budget and the existing category framing is sharper. |
| **"Self-healing code"** | Engineer-speak. Vibe coders do not say "self-healing." |
| **"AI that ships production code"** | "Ships" is on the banned list, and "production" is jargon vibe coders do not respect. |
| **"The end of debugging"** | Hyperbole. Falsifiable on day one. Confident voice does not over-promise. |
| **"Build with confidence"** | Both "build" and "confidence" are saturated. Reads like a Citibank ad. |
| **"AI for AI builders"** | Reflexive cuteness. Forbidden. |
| **"From idea to app — without the rage-quit"** | Almost good. "Rage-quit" is too informal and too narrow. The pain is debugging, not quitting. |

---

*End of Phase 01 positioning document. The next phase touches product before it touches copy. This document does not change until a counter-thesis with evidence is written.*
