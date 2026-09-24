from __future__ import annotations
import json, math, re, os
from pathlib import Path
from typing import Any
from app.rag.rag_service import rag
from app.agents.llm_engine import LLMEngine

llm = LLMEngine()

ROOT=Path(__file__).resolve().parent
DATASET=ROOT/'dataset.jsonl'
RESULTS=ROOT/'results'
RESULTS.mkdir(exist_ok=True)

STOP={'the','a','an','and','or','to','of','in','on','for','is','are','was','were','with','from','this','that','it','as','at','by','be','what','why','did','the','cause','root'}

def tokens(s):
    return {x for x in re.findall(r'[a-z0-9_./:-]+', s.lower()) if x not in STOP and len(x)>2}

def load(): return [json.loads(x) for x in DATASET.read_text().splitlines() if x.strip()]

def retrieval_metrics(rows, k=4):
    hits=rr=[]
    per=[]
    for r in rows:
        q='\n'.join([r['query'],r['failure_type'],r['logs'],r['diff']])
        got=rag.retrieve(q, top_k=k)
        sources=[x['source'] for x in got]
        relevant=set(r['relevant_documents'])
        hit=bool(relevant.intersection(sources))
        rank=next((i+1 for i,s in enumerate(sources) if s in relevant),0)
        precision=len(relevant.intersection(sources))/max(1,len(sources))
        hits.append(hit); rr.append(1/rank if rank else 0)
        per.append({'id':r['id'],'retrieved':sources,'hit_at_k':hit,'reciprocal_rank':1/rank if rank else 0,'precision_at_k':precision})
    return {'recall_at_k':sum(hits)/len(rows),'mrr':sum(rr)/len(rows),'precision_at_k':sum(x['precision_at_k'] for x in per)/len(rows),'per_case':per}

def heuristic_rca(text, expected):
    # Deterministic fallback for running without an API key; measures lexical agreement, not LLM quality.
    a=tokens(text); b=tokens(expected)
    return len(a&b)/max(1,len(b))

def rca_eval(rows, use_rag):
    scores=[]; grounded=[]; per=[]
    for r in rows:
        q='\n'.join([r['query'],r['logs'],r['diff']])
        ctx=rag.format_context(rag.retrieve(q, top_k=4)) if use_rag else 'No external knowledge was provided.'
        if llm.available():
            out=llm.generate(f"Explain the root cause in one sentence.\nLOGS:\n{r['logs']}\nDIFF:\n{r['diff']}\nCONTEXT:\n{ctx}")
            answer=out
        else:
            # Baseline vs RAG: simulate evidence availability by appending retrieved context.
            answer=r['logs']+' '+r['diff']+((' '+ctx) if use_rag else '')
        score=heuristic_rca(answer,r['expected_root_cause'])
        # Groundedness proxy: expected-cause terms present in evidence/context.
        evidence=(r['logs']+' '+r['diff']+((' '+ctx) if use_rag else '')).lower()
        exp_terms=tokens(r['expected_root_cause'])
        ground=len({t for t in exp_terms if t in evidence})/max(1,len(exp_terms))
        scores.append(score); grounded.append(ground)
        per.append({'id':r['id'],'rca_score':score,'groundedness_proxy':ground,'answer':answer[:500]})
    return {'rca_keyword_accuracy':sum(scores)/len(scores),'groundedness_proxy':sum(grounded)/len(grounded),'per_case':per}

def run():
    rows=load()
    retrieval=retrieval_metrics(rows,4)
    off=rca_eval(rows,False)
    on=rca_eval(rows,True)
    summary={
      'dataset_size':len(rows),'rag_top_k':4,'llm_provider':llm.provider if llm.available() else 'deterministic-fallback','llm_model':llm.model if llm.available() else None,
      'retrieval':{k:v for k,v in retrieval.items() if k!='per_case'},
      'without_rag':{k:v for k,v in off.items() if k!='per_case'},
      'with_rag':{k:v for k,v in on.items() if k!='per_case'},
      'improvement':{
        'rca_keyword_accuracy_delta':on['rca_keyword_accuracy']-off['rca_keyword_accuracy'],
        'groundedness_delta':on['groundedness_proxy']-off['groundedness_proxy']
      }
    }
    detail={'summary':summary,'retrieval_cases':retrieval['per_case'],'without_rag_cases':off['per_case'],'with_rag_cases':on['per_case']}
    (RESULTS/'evaluation_results.json').write_text(json.dumps(detail,indent=2))
    (RESULTS/'evaluation_summary.json').write_text(json.dumps(summary,indent=2))
    return summary

if __name__=='__main__': print(json.dumps(run(),indent=2))
