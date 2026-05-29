# Schedule Constraint System

An LLM-assisted Interface to solve scheduling problems using IDP-Z3.

## Setup

1. Install the dependencies:

   ```
   py -m pip install -r requirements.txt
   ```

2. Create OpenRouter API Key

To create an OpenRouter account, go to openrouter.ai and sign up whatever way you prefer, then, if you want to use the paid models we discuss below,  add some credit to your account from the Credits page (a few dollars is largely enough to use this system). Once signed in, open the Keys page from your profile menu, click Create Key, give it a name, and copy it.

3. Setup .env file

Copy `.env.example` to `.env` and fill in:
   - `OPENROUTER_API_KEY` — your active key you just copied from openrouter.ai
   - `MODEL_ID` — the model you want to use (see below)

4. Picking a model

Set `MODEL_ID` in `.env`. During developpment we used 3 models that brought positive results:

 `anthropic/claude-sonnet-4.6`, `anthropic/claude-opus-4.7` and `deepseek/deepseek-v4-flash`.

 None of them are free; the deepseek-v4-flash model is the cheapest: costing 0.10$/M input tokens and 0.20$/M output tokens. It is the model I recommand and is set by default.

## Run

```
py -m uvicorn main:app --reload
```

Then open http://127.0.0.1:8000 in your browser.

## How to use

- always prompt the system through the chat interface.
- when mentionning new workers our people that have a role in the schedule it is better practice to introduce them to the system first, for example : "add Basile and Noé to the team".
- Type constraints or availabilities in the chat, the system can understand multiple constraints given at once, but it is safer to give them out one by one, as in some rare cases the LLM might forget to act on one (especially with cheaper models).
- Click **+ Add Knowledge** to save them.
- Click **Create Schedule** to run the solver and see the timeline.
- If the schedule is infeasible, try out what the system proposes as solution and run create schedule again.

## Problem example

Here is the problem example we used in the video demonstration:

Availabilities :

- fleming is available on friday saturday and Sunday
- freud is available everey day besides on evenign shifts
- Heimlich is available every day but never the night shift on weekends
- eustachi is available every day every shift
- golgi is available every day, every shift but at max 2 night shifts

constraints :

- a doctor can only work one shift per day
- a doctor should Always be available for his shift
- if a doctor ahs the night shift, they either get the next day off, or the night shift again
- a doctor either works both days of the weekend or none of the days
