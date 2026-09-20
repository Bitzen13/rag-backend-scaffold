# What is Retrieval-Augmented Generation?

Retrieval-Augmented Generation (RAG) is a technique that grounds a large language model's answers in
external source material. Instead of relying only on what the model memorised during training, a RAG
system first retrieves the passages most relevant to the user's question and then asks the model to
answer using only those passages.

## Why use it?

RAG reduces fabricated answers because the model is instructed to stay within the retrieved context.
It also keeps answers current: updating the underlying documents updates the answers, with no model
retraining. Every answer can point back to the passages it was built from, so users can verify it.

## The two phases

Ingestion prepares the knowledge base. Documents are extracted into clean text, split into chunks,
converted into embedding vectors, and stored in a vector database along with metadata.

Querying happens at request time. The question is embedded, the most similar chunks are retrieved,
a prompt combining those chunks with the question is built, and the language model produces a
grounded answer with source references.
