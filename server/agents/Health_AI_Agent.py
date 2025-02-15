""" This module contains the HealthAIAgent class, which is a subclass of AIAgent."""
import os
import logging
import json
import base64
import subprocess
import asyncio
from datetime import datetime
from operator import itemgetter

# LangChain / langchain_core
from langchain.chains import LLMChain
from langchain.memory.chat_memory import BaseChatMemory
from langchain.agents import initialize_agent, AgentType
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.chat_history import BaseChatMessageHistory
from langchain.memory.summary import ConversationSummaryMemory
from langchain_core.messages import trim_messages
from langchain_core.messages.human import HumanMessage
from langchain_core.messages.system import SystemMessage
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.runnables import RunnablePassthrough

# MongoDB Chat
from langchain_mongodb.chat_message_histories import MongoDBChatMessageHistory

# Pydub for audio
from pydub import AudioSegment

# Custom modules
from .ai_agent import AIAgent
from services.azure_mongodb import MongoDBClient
from services.azure_form_recognizer import extract_text_from_file
from services.text_to_speech_service import text_to_speech
from models.user import User
from services.db.user import get_user_profile_by_user_id
from utils.consts import SYSTEM_MESSAGE
from utils.agents import fetch_meme  # or as needed

class HealthAIAgent(AIAgent):
    """
    A class that retains user mood logic, memory stubs, and advanced functionality,
    but uses OPENAI_FUNCTIONS to avoid streaming with tool calls.
    """

    def __init__(
        self,
        system_message: str = SYSTEM_MESSAGE,
        tool_names: list[str] = [],
        desired_role: str = "MemeMingle"
    ):
        """
        Initializes a MemeMingleAIAgent object.
        """
        self.desired_role = desired_role
        formatted_system_message = system_message.format(role=desired_role)
        self.system_message = SystemMessage(content=formatted_system_message)

        super().__init__(formatted_system_message, tool_names)

        # This prompt skeleton includes placeholders for 'past_summaries' and others
        # plus old "MessagesPlaceholder" for advanced memory usage if needed
        self.base_prompt_messages = [
            ("system", self.system_message.content),
            ("system", "{past_summaries}"),
            ("system", "You can retrieve information about the AI using the 'agent_facts' tool."),
            ("system", "You can generate suggestions using the 'generate_suggestions' tool."),
            ("system", "You can search for information using the 'web_search_bing' tool."),
            ("system", "You can search for textbook PDFs using the 'textbook_search' tool."),
            ("system", "You can search for textbooks using the 'gutendex_textbook_search' tool."),
            ("system", "You can search for information using the 'web_search_tavily' tool."),
            ("system", "You can search for locations using the 'location_search_gplaces' tool."),
            ("system", "You can retrieve your user profile using the 'user_profile_retrieval' tool."),
            ("system", "You can generate documents using the 'generate_document' tool."),
            ("system", "You can fetch popular memes using the 'fetch_meme' tool."),
            ("system", "user_id:{user_id}"),       
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),  # If your chain uses an internal scratchpad
        ]

        # Directory for generated audio
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, ".."))
        self.generated_audio_dir = os.path.join(project_root, "generated_audio")
        os.makedirs(self.generated_audio_dir, exist_ok=True)

        # We'll create the agent later (non-streaming). 
        self.agent_executor = None

    # ---------------------------------------------------------------------
    # 1) GET/SET Chat History & Memory
    # ---------------------------------------------------------------------
    def get_session_history(self, session_id: str) -> MongoDBChatMessageHistory:
        """
        Retrieves chat history for a session from MongoDB.
        """
        CONNECTION_STRING = MongoDBClient.get_mongodb_variables()
        history = MongoDBChatMessageHistory(
            CONNECTION_STRING,
            session_id,
            MongoDBClient.get_db_name(),
            collection_name="chat_turns"
        )
        logging.info(f"Retrieved chat history for session {session_id}")
        return history or []

    def get_agent_memory(self, user_id: str, chat_id: int) -> BaseChatMemory:
        """
        Placeholder for memory if you want to store summary or expansions.
        """
        # Example usage, if you had a summary memory chain:
        # memory = ConversationSummaryMemory(llm=self.llm, chat_memory=..., return_messages=True)
        return None

    def get_agent_with_history(self, executor) -> RunnableWithMessageHistory:
        """
        Wrap the agent (executor) with RunnableWithMessageHistory
        for reading/writing conversation from Mongo.
        """
        agent_with_history = RunnableWithMessageHistory(
            executor,
            get_session_history=self.get_session_history,
            input_messages_key="input",
            history_messages_key="chat_turns",
            verbose=True
        )
        return agent_with_history

    # ---------------------------------------------------------------------
    # 2) OLD MOOD LOGIC
    # ---------------------------------------------------------------------
    def get_user_mood(self, user_id, chat_id) -> str:
        """
        Analyzes session history from MongoDB to detect user's mood in a single adjective.
        """
        history: BaseChatMessageHistory = self.get_session_history(f"{user_id}-{chat_id}")
        history_log = asyncio.run(history.aget_messages())

        # We'll ask the LLM to interpret the mood:
        instructions = """
        Given the messages provided, describe the user's mood in a single adjective.
        If uncertain, return 'None'.
        """

        # Build an ephemeral prompt
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", instructions),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        # Trim messages if they're too large
        trimmer = trim_messages(
            max_tokens=65,
            strategy="last",
            token_counter=self.llm,
            include_system=True,
            allow_partial=False,
            start_on="human",
        )
        trimmer.invoke(history_log)

        chain = RunnablePassthrough.assign(messages=itemgetter("messages") | trimmer) | prompt | self.llm
        response = chain.invoke({"messages": history_log})
        mood = response.content.strip()
        if mood == "None":
            mood = "neutral"
        logging.info(f"The user is feeling: {mood}")
        return mood

    # ---------------------------------------------------------------------
    # 3) Chat ID Helper
    # ---------------------------------------------------------------------
    @staticmethod
    def get_chat_id(user_id):
        """
        Returns the most recent chat_id for a given user from the "chat_summaries" collection.
        """
        db_client = MongoDBClient.get_client()
        db_name = MongoDBClient.get_db_name()
        db = db_client[db_name]
        chat_summary_collection = db["chat_summaries"]

        most_recent_chat_summary = chat_summary_collection.find_one(
            {"user_id": user_id},
            sort=[("chat_id", -1)]
        )
        if most_recent_chat_summary:
            return most_recent_chat_summary.get("chat_id")
        return None

    # ---------------------------------------------------------------------
    # 4) INITIALIZE AGENT with AgentType.OPENAI_FUNCTIONS (NO streaming)
    # ---------------------------------------------------------------------
    def _initialize_agent_executor(self):
        """
        Creates a non-streaming function-calling agent with OPENAI_FUNCTIONS.
        """
        if self.agent_executor is not None:
            return  # Only create once

        # Instead of create_tool_calling_agent, we do:
        self.agent_executor = initialize_agent(
            tools=self.tools,
            llm=self.llm,
            agent=AgentType.OPENAI_FUNCTIONS,  # Key to no streaming
            verbose=True,
            handle_parsing_errors=True
        )

    # ---------------------------------------------------------------------
    # 5) MAIN RUN METHOD
    # ---------------------------------------------------------------------
    def run(
        self,
        message: str,
        file_content: bytes = None,
        file_mime_type: str = None,
        with_history: bool = True,
        user_id: str = None,
        chat_id: int = None,
        turn_id: int = None
    ) -> dict:
        """
        Primary method: processes user input, optionally file content,
        returns a dict with AI text + meme + audio, etc.
        """
        # If chat_id not provided, fetch the most recent from DB
        if chat_id is None:
            chat_id = self.get_chat_id(user_id)

        session_id = f"{user_id}-{chat_id}"

        # Grab past conversation summaries from DB
        db_client = MongoDBClient.get_client()
        db_name = MongoDBClient.get_db_name()
        db = db_client[db_name]
        chat_summary_collection = db["chat_summaries"]

        past_summaries_cursor = chat_summary_collection.find({"user_id": user_id}).sort("chat_id", -1)
        past_summaries = list(past_summaries_cursor)
        summaries_text = "\n".join([summary.get("summary_text", "") for summary in past_summaries])

        # If we have a file, extract text
        extracted_text = ""
        if file_content and file_mime_type:
            extracted_text = extract_text_from_file(file_content, file_mime_type)
            logging.info(f"Extracted text from file: {extracted_text[:500]}")

        # Build final set of system/human messages
        prompt_messages = self.base_prompt_messages.copy()
        if extracted_text:
            # Insert an extra system message describing the file content
            prompt_messages.insert(
                2,
                ("system", f"The user has provided a document:\n{extracted_text}")
            )

        # Turn these messages into a ChatPromptTemplate
        prompt = ChatPromptTemplate.from_messages(prompt_messages)

        # Render the final prompt
        rendered_prompt = prompt.format(
            input=message,
            user_id=user_id,
            past_summaries=summaries_text,
            agent_scratchpad= [],
        )

        # Initialize our function-calling agent if not done already
        self._initialize_agent_executor()

        # If you want to wrap with memory/history, do so:
        if with_history:
            self.agent_executor = self.get_agent_with_history(self.agent_executor)
            result = self.agent_executor.invoke(
                {"input": rendered_prompt},
                config={"configurable": {"session_id": session_id}}
            )
            ai_text_response = result["output"]
        else:
            ai_text_response = self.agent_executor.run(rendered_prompt)


        try:

            # If it's the first turn, is_initial = True
            is_initial = (turn_id == 0)

            # Meme logic
            meme_topic = self.determine_meme_topic(ai_text_response, is_initial=is_initial)
            fetch_meme_tool = self.get_tool_by_name("fetch_meme")
            meme_url = fetch_meme_tool.func(meme_topic) if fetch_meme_tool else None

            # TTS
            audio_url = self.convert_text_to_speech(ai_text_response, user_id, chat_id, turn_id)


            # Return structured
            return {
                "message": ai_text_response,
                "meme_url": meme_url,
                "audio_url": audio_url,
            }

        except Exception as e:
            logging.error(f"Error during agent execution: {e}", exc_info=True)
            raise

    # ---------------------------------------------------------------------
    # 6) TTS + Meme + Lipsync + Expressions (unchanged from old)
    # ---------------------------------------------------------------------
    def get_tool_by_name(self, tool_name: str):
        """
        Retrieves a tool object by its name from self.tools.
        """
        for tool in self.tools:
            if tool.name == tool_name:
                return tool
        return None

    def determine_meme_topic(self, ai_response: str, is_initial: bool = False) -> str:
        """
        Asks LLM to decide on a meme topic, narrower if is_initial=True.
        """
        if is_initial:
            prompt = (
                "Analyze the following AI response and pick the best welcoming meme topic from the list:\n"
                "welcome, hello, introduction, greeting.\n\n"
                f"AI response:\n{ai_response}\n\n"
                "Return only the topic."
            )
        else:
            prompt = (
                "Analyze the AI response and determine the best meme topic. Return only the topic.\n\n"
                f"{ai_response}"
            )
        try:
            response = self.llm.invoke(prompt)
            return response.content.strip().lower()
        except Exception as e:
            logging.error(f"Meme topic determination failed: {e}")
            return "funny"


    def convert_text_to_speech(self, text: str, user_id: str, chat_id: int, turn_id: int) -> tuple:
        """
        Converts text to TTS WAV, returning (audio_url, filename).
        """
        try:
            user = User.find_by_id(user_id)
            if not user:
                logging.warning(f"User with ID {user_id} not found.")
                return "", ""

            preferred_language = user.preferredLanguage or "en"
            audio_data = text_to_speech(text, preferred_language=preferred_language)
            if not audio_data:
                raise ValueError("No audio data from TTS.")

            filename = f"{user_id}_{chat_id}_{turn_id}.wav"
            file_path = os.path.join(self.generated_audio_dir, filename)
            with open(file_path, "wb") as f:
                f.write(audio_data)

            backend_base_url = os.getenv("BACKEND_BASE_URL", "http://localhost:8000")
            audio_url = f"{backend_base_url}/ai_mentor/download_audio/{filename}"
            return audio_url
        except Exception as e:
            logging.error(f"TTS conversion error: {e}")
            return "", ""

    
    

    # ---------------------------------------------------------------------
    # 7) Summarization & Finalization
    # ---------------------------------------------------------------------
    def get_summary_from_chat_history(self, user_id, chat_id):
        """
        Summarizes the entire conversation using ConversationSummaryMemory
        (or a simpler approach if needed).
        """
        history: BaseChatMessageHistory = self.get_session_history(f"{user_id}-{chat_id}")
        memory = ConversationSummaryMemory(
            llm=self.llm,
            chat_memory=history,
            return_messages=False,
            input_key="input",
            output_key="output"
        )

        messages = asyncio.run(history.aget_messages())

        # Pair up messages in (human, AI) if possible
        for i in range(0, len(messages), 2):
            if i + 1 < len(messages):
                human_msg = messages[i]
                ai_msg = messages[i + 1]
                if isinstance(human_msg, HumanMessage) and not isinstance(ai_msg, HumanMessage):
                    memory.save_context({"input": human_msg.content}, {"output": ai_msg.content})

        summary = memory.load_memory_variables({}).get("history", "")
        logging.info(f"Generated summary: {summary}")
        return summary

    def perform_final_processes(self, user_id, chat_id):
        """
        Example final step: gather mood + conversation summary,
        update DB "chat_summaries" with perceived_mood + summary_text.
        """
        db_client = MongoDBClient.get_client()
        db_name = MongoDBClient.get_db_name()
        db = db_client[db_name]

        chat_summary_collection = db["chat_summaries"]
        mood = self.get_user_mood(user_id, chat_id)
        summary = self.get_summary_from_chat_history(user_id, chat_id)

        chat_summary_collection.update_one(
            {"user_id": user_id, "chat_id": int(chat_id)},
            {"$set": {"perceived_mood": mood, "summary_text": summary}}
        )
        logging.info(f"perform_final_processes: updated mood={mood}, summary length={len(summary)} for chat={chat_id}.")

    # ---------------------------------------------------------------------
    # 8) get_initial_greeting
    # ---------------------------------------------------------------------
    def get_initial_greeting(self, user_id: str) -> dict:
        db_client = MongoDBClient.get_client()
        db_name = MongoDBClient.get_db_name()
        db = db_client[db_name]

        user_journey_collection = db["user_journeys"]
        chat_summary_collection = db["chat_summaries"]
        user_journey = user_journey_collection.find_one({"user_id": user_id})

        user_profile_json = get_user_profile_by_user_id(user_id)
        if user_profile_json:
            user_profile = json.loads(user_profile_json)
        else:
            user_profile = {}

        print("user_profile", user_profile)
        preferred_language = user_profile.get("preferredLanguage", "en")

        # Add language enforcement instruction
        language_instruction = (
            f"\n\n**Language Enforcement:** Respond exclusively in {preferred_language} using the correct script. "
            "Do not use any other language, even if the user writes in another language. "
            "All responses must be in {preferred_language} without exception."
        )
        self.system_message.content += language_instruction

        # (Existing logic)
        past_summaries_cursor = chat_summary_collection.find({"user_id": user_id}).sort("chat_id", -1)
        past_summaries = list(past_summaries_cursor)
        recent_summaries = past_summaries[:2]
        summaries_text = "\n".join([summary.get("summary_text", "") for summary in recent_summaries])
        if summaries_text:
            self.system_message.content += f"\nPrevious Summaries:\n{summaries_text}"

        now = datetime.now()
        chat_id = int(now.timestamp())

        chat_summary_collection.insert_one({
            "user_id": user_id,
            "chat_id": chat_id,
            "desired_role": self.desired_role,
            "perceived_mood": "",
            "summary_text": "",
            "concerns_progress": []
        })

        if not user_journey:
            user_journey_collection.insert_one({
                "user_id": user_id,
                "patient_goals": [],
                "therapy_type": [],
                "last_updated": datetime.now().isoformat(),
                "therapy_plan": [],
                "mental_health_concerns": []
            })
            introduction = """
    This is your first session with the user. Be polite and introduce yourself in a warm, empathetic, and inviting manner.

    In this session, aim to understand the user's health and wellness objectives. Ask open-ended questions to uncover any key concerns or goals—whether they pertain to nutrition, mental well-being, fitness, or other aspects of a healthy lifestyle.

    **Important Disclaimer:**
    - You are here to provide general health and wellness advice, not to replace professional medical care. Encourage the user to seek a qualified healthcare provider for medical diagnosis and treatment where needed.

    **Language Enforcement:**
    - The user’s preferred language is {preferred_language}.
    - You must respond exclusively in {preferred_language}, regardless of the user’s initial greeting language.
    - For example, if the user’s profile says “zh” for Chinese, all messages should be in Chinese. If the user’s profile says “fr,” respond in French, etc.
    - If you genuinely cannot continue in {preferred_language}, apologize in {preferred_language} and ask for clarification.


    Explain that you are here to support their wellness journey by offering evidence-based tips, mental health encouragement, lifestyle recommendations, and motivational guidance. Ensure the user feels welcomed, understood, and optimistic about improving their overall well-being.
    """
            self.system_message.content += introduction

        # Then run the first message with turn_id=0
        response = self.run(
            message="",
            with_history=True,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=0
        )

        return {
            "message": response,
            "chat_id": chat_id
        }