"""Compare local semantic encoders on a fixed skill retrieval corpus.

Run with the project's Python after installing the selected optional backend:
  python scripts/benchmark_skill_retrieval.py --backend fastembed --model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
  python scripts/benchmark_skill_retrieval.py --backend sentence-transformers --model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2

The development split chooses a confidence threshold. The held-out split is
reported once with that threshold and is never used to tune it.
"""
import argparse
import json
import math
import os
import time
from pathlib import Path


CORPUS = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'skill_retrieval_cases.json'


def encode(backend, model_name, texts):
    if backend == 'fastembed':
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=model_name)
        return [list(map(float, row)) for row in model.embed(texts)]
    if backend == 'sentence-transformers':
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_name, device='cpu')
        return [list(map(float, row)) for row in model.encode(texts)]
    raise ValueError('Unknown backend')


def cosine(left, right):
    if len(left) != len(right) or not left:
        raise ValueError('Embedding dimension mismatch')
    norm = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right)) / norm if norm else 0.0


def rankings(skills, cases, vectors):
    skill_vectors = vectors[:len(skills)]
    query_vectors = vectors[len(skills):]
    result = []
    for case, query in zip(cases, query_vectors):
        matches = sorted(((cosine(query, vector), skill['id'])
                          for skill, vector in zip(skills, skill_vectors)), reverse=True)
        result.append({'query': case['query'], 'expected': case['expected'],
                       'top_id': matches[0][1], 'score': matches[0][0],
                       'margin': matches[0][0] - matches[1][0] if len(matches) > 1 else 1.0})
    return result


def choose_threshold(rows):
    scores = sorted({row['score'] for row in rows})
    candidates = [-1.0, 1.0] + [(a + b) / 2 for a, b in zip(scores, scores[1:])]
    # Break ties toward the higher threshold to reduce false automatic reuse.
    return max(candidates, key=lambda threshold: (
        sum((row['top_id'] if row['score'] >= threshold else None) == row['expected'] for row in rows),
        threshold))


def report(rows, threshold):
    decisions = []
    for row in rows:
        selected = row['top_id'] if row['score'] >= threshold else None
        decisions.append({**row, 'selected': selected, 'correct': selected == row['expected']})
    return {'total': len(rows), 'correct': sum(row['correct'] for row in decisions),
            'false_reuse': sum(row['expected'] is None and row['selected'] is not None for row in decisions),
            'missed_reuse': sum(row['expected'] is not None and row['selected'] is None for row in decisions),
            'cases': decisions}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=['fastembed', 'sentence-transformers'], required=True)
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
    skills = corpus['skills']
    cases = corpus['development'] + corpus['held_out']
    texts = [skill['description'] for skill in skills] + [case['query'] for case in cases]
    start = time.perf_counter()
    vectors = encode(args.backend, args.model, texts)
    elapsed = time.perf_counter() - start
    development = rankings(skills, corpus['development'], vectors[:len(skills) + len(corpus['development'])])
    held_out_vectors = vectors[:len(skills)] + vectors[len(skills) + len(corpus['development']):]
    held_out = rankings(skills, corpus['held_out'], held_out_vectors)
    threshold = choose_threshold(development)
    try:
        import psutil
        memory_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 ** 2
    except ImportError:
        memory_mb = None
    print(json.dumps({'backend': args.backend, 'model': args.model, 'threshold': threshold,
                      'seconds_total': round(elapsed, 3), 'memory_rss_mb': memory_mb,
                      'development': report(development, threshold),
                      'held_out': report(held_out, threshold)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
