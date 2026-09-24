# Knowledge base retrieval

Retrieval ingests Markdown and text files from `knowledge_base/`, chunks
them, and (when `sentence-transformers` is installed) creates local semantic
embeddings with `all-MiniLM-L6-v2`. Without that optional dependency,
retrieval falls back to a deterministic lexical overlap score, so the gate
remains fully runnable with zero downloads and zero external calls.

Retrieved context is handed to the Retrieval and Root Cause agents as
background material only. It is explicitly labelled as non-authoritative:
the current CI logs and diff are the only evidence the root-cause summary is
allowed to be grounded in, so a past incident about a timeout cannot get
mistaken for the cause of today's assertion failure.
