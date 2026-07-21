# views/refinery/batch_exceptions.py
class BatchCancelledError(Exception):
    """
    Raised when st.session_state.cancel_batch is True at a cancel checkpoint
    inside a strategy sub-loop.

    This exception is caught by _process_single_job's except clause, which
    sets job['status'] = 'Cancelled' (not 'Failed').
    """
    pass
