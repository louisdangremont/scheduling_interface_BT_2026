# Schedule Constraint System

A constraint-driven scheduler powered by IDP-Z3 and an LLM front-end.
You type constraints in plain English, the LLM translates them to FO(.) logic, and IDP-Z3 builds the schedule.

## Setup

1. Install the dependencies:

   ```
   py -m pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in:
   - `OPENROUTER_API_KEY` — your active key from [openrouter.ai](https://openrouter.ai/)
   - `MODEL_ID` — the model you want to use (see below)

## Run

```
py -m uvicorn main:app --reload
```

Then open http://127.0.0.1:8000 in your browser.

## Picking a model

Set `MODEL_ID` in `.env`. A free model you can try:

```
MODEL_ID=nvidia/nemotron-3-super-120b-a12b:free
```

Any OpenRouter-compatible model ID works (e.g. `anthropic/claude-sonnet-4.6`, `deepseek/deepseek-v4-flash`).

## How to use

- Type constraints or availabilities in the chat
- Click **+ Add Knowledge** to save them
- Click **Create Schedule** to run the solver and see the timeline
