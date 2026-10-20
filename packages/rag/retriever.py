from typing import List, Dict, Any
from rag.indexer import Indexer
from rag.isolation import build_client_namespace
import re

class Retriever:
    def __init__(self, indexer: Indexer):
        self.indexer = indexer

    def retrieve(self, client_id: str, gstin: str, vendor_name: str, descriptions: List[str], hsn: str, amount_band: str, doc_type: str, top_k: int = 8) -> List[Dict[str, Any]]:
        # This will simulate an embedding search by just fetching all items from the correct namespace and returning a slice
        # The key constraint is hard isolation by client_id.
        namespace = build_client_namespace(client_id)
        
        # In a real app we'd build a query string and do cosine similarity over the vector DB namespace
        # query_str = f"{gstin} {vendor_name} {' '.join(descriptions)} {hsn} {amount_band} {doc_type}"
        
        chunks = self.indexer.get_namespace_chunks(client_id)
        
        scored_chunks = []
        for c in chunks:
            content = c["content"].lower()
            metadata = c.get("metadata", {})
            score = 0.0
            reasons = []
            def add(reason, points, matched):
                nonlocal score
                if matched:
                    score += points
                    reasons.append(reason)
            add("gstin", 8, bool(gstin) and (gstin.lower() in content or metadata.get("gstin") == gstin))
            add("vendor_name", 5, bool(vendor_name) and vendor_name.lower() in (content + " " + str(metadata).lower()))
            terms = {term for desc in descriptions for term in re.findall(r"[a-z0-9]+", desc.lower()) if len(term) > 2}
            add("description", min(4, len(terms & set(re.findall(r"[a-z0-9]+", content)))), bool(terms & set(re.findall(r"[a-z0-9]+", content))))
            add("hsn", 4, bool(hsn) and (hsn in content or str(metadata.get("hsn", "")).startswith(hsn)))
            try:
                query_amount, chunk_amount = float(amount_band), float(metadata.get("amount", 0))
                amount_match = chunk_amount > 0 and abs(query_amount - chunk_amount) <= max(1, query_amount * .2)
            except (TypeError, ValueError):
                amount_match = False
            add("amount", 3, amount_match)
            add("doc_type", 2, bool(doc_type) and (doc_type.lower() in content or metadata.get("doc_type") == doc_type))
            if c["chunk_type"] == "memory": score += 2
            if c["chunk_type"] == "posted_bill": score += 1
            ranked = dict(c)
            ranked["score"] = score
            ranked["ranking_reasons"] = reasons
            scored_chunks.append((score, ranked))
            
        scored_chunks.sort(key=lambda x: (-x[0], x[1]["chunk_id"]))
        
        # Return top_k
        return [c for score, c in scored_chunks[:top_k]]
