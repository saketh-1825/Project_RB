import logging
from schemas.state import AnalysisState

logger = logging.getLogger(__name__)

def human_review_node(state: AnalysisState) -> AnalysisState:
    """
    Dedicated Human Review Node. Sets human review fields on AnalysisState for low-confidence routing
    and halts the graph execution, waiting for operator context.
    """
    logger.info("Confidence score is low. Transitioning to Human Review Node...")
    
    # Calculate review reason from confidence score reasoning if available
    confidence_reason = state.get("correlation", {}).get("confidence", {}).get("reason", "Confidence score below threshold")
    
    # Inject required fields
    state["status"] = "awaiting_human"
    state["waiting_at"] = "confidence_review"
    state["review_reason"] = confidence_reason
    state["requires_input"] = True
    
    # Preserve backward compatibility for existing tests/routers
    state["awaiting_human"] = True
    state["interrupt_type"] = "confidence_review"
    state["interrupt_question"] = "Confidence score is below threshold. Please review the collected evidence and provide context."
    
    return state
