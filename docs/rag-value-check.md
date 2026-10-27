# RAG value check: knowledge base attached vs detached

Date: 2026-10-27. Purpose: understand what the Lyzr knowledge base adds beyond `read_file`, as called for by
the PRD ("RAG value check"). Nothing was removed or changed; the KB was only detached from the agent in
Studio for the second run and its documents were left in place.

## Setup

- Repo: [adiletbaimyrza/react-typescript-todo-app](https://github.com/adiletbaimyrza/react-typescript-todo-app) in `sandbox/`
  (17 `.tsx`, 6 `.ts`, about 1,143 lines; 39 files, 31 indexed in the KB).
- Agent: "Repo Steward" with the four tools. The only difference between runs was the KB attachment.
- Method: the same three read-only questions sent through the backend's `/chat`, each in a fresh session.
  Tool calls are counted from the backend audit log (`actor = agent` rows during each question); time is the
  wall-clock duration of the `/chat` call.
- Raw replies and tool-call lists: [kb_attached.json](rag-value-check/kb_attached.json),
  [kb_detached.json](rag-value-check/kb_detached.json).

## Ground truth (from the sandbox)

1. Zustand store: `src/apps/Zustand/store.ts`, exports `useStore` (`todoList`, `setTodoList`).
2. Redux completion: `completeHandler` in `src/apps/Redux/index.tsx` maps the list and dispatches the slice's
   `set` action (`src/apps/Redux/TodoListSlice.ts`); the button lives in `src/apps/Redux/Todo.tsx`.
   The answer should cite only Redux files.
3. Textarea importers: the `AddTodo.tsx` and `Todo.tsx` files of the Redux, UseState and Zustand apps (6 files).

## Results

| Question | KB attached | KB detached |
|---|---|---|
| 1. Zustand store | 1 call, 9.4s | 7 calls, 14.5s |
| 2. Redux completion | 2 calls, 9.3s | 9 calls, 19.0s |
| 3. Textarea users | 7 calls, 11.4s | 16 calls, 28.8s |
| **Total** | **10 calls, 30.1s** | **32 calls, 62.3s** |
| Correct, right files cited | 3 of 3 | 3 of 3 |

## Observations

- **Accuracy was the same.** Both runs found `store.ts` and `useStore`, cited only Redux files for question 2, and
  listed exactly the 6 `Textarea` importers.
- **With the KB the agent went straight to the relevant files.** Without it, it walked the folders first:
  4 directory listings before reading anything for question 1, 4 for question 2 and 8 for question 3.
  Question 2 also read `TodoListSlice.ts` twice.
- **Detached answers were slightly richer.** Question 1 added the `/zustand` route in `src/App.tsx`, and question 2
  explained the reducer. Browsing surfaced extra context; the attached answers were narrower.
- **Attached runs still verified with `read_file`**, as the system prompt requires, so the KB narrowed the search
  without replacing the files as the source of truth.

## Conclusion

At this repo size the KB improves efficiency, not correctness: about 3x fewer tool calls and about 2x faster
answers. The cost of folder browsing grows with the number of files (question 3 needed 16 calls for 23 files),
so the KB's value should increase on larger repos. It adds ingest, upsert and rebuild-on-delete work, which is
the trade-off to weigh.

## Caveats

- One run per condition, so some of the gap may be run-to-run variation. Trust the direction (fewer calls,
  less time with the KB) more than the exact ratio.
- Small repo; larger repos and mixed-language repos were not tested.
- Edits and deletes, and how fresh the KB is after approved changes, were not part of this check.
- A grep-style `search_code` tool would likely recover most of the call savings without KB syncing. It was not built
  or measured.
