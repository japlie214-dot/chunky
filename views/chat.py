# views/chat.py
# Phase 3: Chat Playground View Module
import streamlit as st
import pandas as pd
import json
from snowflake.snowpark.context import get_active_session
from logger_config import log_action
from utils.snowflake_utils import retrieve_context, generate_llm_response, process_monitoring_batch

def render_chat_view():
    """Render the Chat Playground view"""
    st.title("🧠 RAG Playground")
    log_action("NAVIGATE", "Visited Chat Playground")
    
    # Phase 3: Chat History Rendering
    st.subheader("💬 Chat Interface")
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
    
    # Chat input logic
    config = st.session_state.active_config
    if config:
        prompt = st.chat_input("Ask a question...")
        if prompt:
            session = get_active_session()
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"): 
                st.markdown(prompt)
            
            with st.chat_message("assistant"):
                m_placeholder = st.empty()
                
                # Retrieve context from RAG
                full_context_chunks, retrieval_meta = retrieve_context(session, config, prompt)
                
                # Build XML prompt
                context_str = "\n\n".join(full_context_chunks)
                history_str = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in st.session_state.messages[:-1]])

                xml_prompt = f"""
<sys_prompt>{config['sys_prompt']}</sys_prompt>
<chat_history>{history_str}</chat_history>
<rag>{context_str}</rag>
<latest_message>{prompt}</latest_message>
"""

                # Safety check (200k char limit)
                if len(xml_prompt) > 200000:
                    err = "Error: Too much context. Lower retrieval limit."
                    m_placeholder.error(err)
                    st.session_state.messages.append({"role": "assistant", "content": err, "retrieval_data": retrieval_meta})
                    log_action("CHAT_ERROR", {"error": "Context too long", "prompt_length": len(xml_prompt)})
                else:
                    try:
                        model_name = config["model"]
                        temp = config.get("temperature", 0.7)
                        top_p_val = config.get("top_p", 0.5)

                        # Generate LLM response
                        result = generate_llm_response(session, xml_prompt, model_name, temp, top_p_val)
                        res_text = result["text"]
                        usage_data = result["usage"]
                        parsing_success = result["parsing_success"]
                        raw_res = result["raw_response"]
                        resp_data = result["resp_data"]

                        # Display response
                        m_placeholder.markdown(res_text)

                        # Conditional debug for parsing issues
                        if not parsing_success or "[Warning:" in res_text:
                            with st.expander("🔍 Debug: Full Raw LLM Response JSON", expanded=True):
                                st.warning("⚠️ Payload parsing issue or empty response detected.")
                                st.json(resp_data)
                                st.caption("Raw string before parsing:")
                                st.code(raw_res[:2000] + ("..." if len(raw_res) > 2000 else ""))

                        # Append to chat history
                        st.session_state.messages.append({"role": "assistant", "content": res_text, "retrieval_data": retrieval_meta})

                        # Create current turn record
                        current_turn = {
                            "user_query": prompt,
                            "bot_response": res_text,
                            "rag_context": context_str,
                            "usage": usage_data,
                            "metadata": {
                                "model": config["model"],
                                "temp": temp,
                                "top_p": top_p_val,
                                "limit": config["limit"]
                            },
                            "timestamp": pd.Timestamp.now().isoformat()
                        }
                        st.session_state.pending_batch.append(current_turn)

                        # Process batch when 5 turns accumulated
                        if len(st.session_state.pending_batch) >= 5:
                            batch_record = process_monitoring_batch(session, st.session_state.pending_batch)
                            if batch_record:
                                st.session_state.monitoring_logs.append(batch_record)
                            st.session_state.pending_batch = []
                            st.success("✅ Batch processed for monitoring!")

                    except Exception as e:
                        st.error(f"LLM Error: {e}")
                        log_action("CHAT_ERROR", {"error": str(e)})
    else:
        st.info("👈 Please configure and apply settings in the sidebar to start chatting.")
    
    # Retrieval Inspector Rendering
    st.markdown("---")
    st.header("🔎 Retrieval Inspector")
    st.caption("Visualizing the raw search results underlying the conversation above.")
    
    # Loop through messages to mimic flow
    if st.session_state.messages:
        for i, msg in enumerate(st.session_state.messages):
            
            # Render User Query
            if msg["role"] == "user":
                with st.chat_message("user", avatar="👤"):
                    st.write(f"**Query:** {msg['content']}")
            
            # Render Retrieved Results
            elif msg["role"] == "assistant":
                with st.chat_message("assistant", avatar="🤖"):
                    retrieval_data = msg.get("retrieval_data", [])
                    
                    if retrieval_data:
                        df_results = pd.DataFrame(retrieval_data)
                        st.dataframe(
                            df_results[["Service", "cosine_similarity", "text_match", "Full Text"]],
                            column_config={
                                "cosine_similarity": st.column_config.NumberColumn(format="%.4f"),
                                "text_match": st.column_config.NumberColumn(format="%.4f"),
                                "Full Text": st.column_config.TextColumn(width="large")
                            },
                            use_container_width=True, hide_index=True
                        )
                        
                        with st.expander("View Full Raw Details"):
                            raw_json = df_results.drop(columns=["Raw @scores"], errors="ignore").to_dict(orient="records")
                            st.json(raw_json)
                    else:
                        st.info("No chunks retrieved.")
    else:
        st.info("Start a conversation to see retrieval details here.")