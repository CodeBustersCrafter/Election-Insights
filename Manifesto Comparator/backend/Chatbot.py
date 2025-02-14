import os
import asyncio
import time
from openai import AsyncOpenAI
from dotenv import load_dotenv
from tavily import Client as TavilyClient
from langchain.memory import ConversationBufferMemory
import google.generativeai as genai
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
from huggingface_hub import InferenceClient
from openai import OpenAI

load_dotenv()

similarity_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')

def initialize_clients():
    LLAMA_api_key = os.getenv("LLAMA_API_KEY")
    if not LLAMA_api_key:
        raise ValueError("LLAMA is not set in the environment variables.")
    LLAMA_client = AsyncOpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=LLAMA_api_key
    )
    
    tavily_api_key = os.getenv("TAVILY_API_KEY")
    if not tavily_api_key:
        raise ValueError("TAVILY_API_KEY is not set in the environment variables.")
    tavily_client = TavilyClient(tavily_api_key)
    
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("GEMINI_API_KEY is not set in the environment variables.")
    genai.configure(api_key=gemini_api_key)
    
    generation_config = {
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "max_output_tokens": 4096,
        "response_mime_type": "text/plain",
    }
    
    gemini_model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config=generation_config,
    )    

    memory = ConversationBufferMemory(return_messages=True)
    
    return LLAMA_client, tavily_client, memory, gemini_model

async def retrieve_context(query, vector_store, top_k=10):
    results = vector_store.similarity_search_with_score(query, k=top_k)
    weighted_context = ""
    for doc, score in results:
        weighted_context += f"{doc.page_content} (relevance: {score})\n\n"
    return weighted_context

def compute_similarity(text1, text2):
    embeddings = similarity_model.encode([text1, text2])
    similarity = cosine_similarity([embeddings[0]], [embeddings[1]])[0][0]
    return similarity

def check_consistency(response1, response2, threshold=0.75):
    similarity = compute_similarity(response1, response2)
    return similarity >= threshold

def aggregate_responses(response1, response2, is_consistent):
    if is_consistent:
        aggregated = f"{response1}\n\nAdditionally, {response2}"
    else:
        aggregated = max(response1, response2, key=len)
    return aggregated

async def fast_check_response(prompt, tavily_client):
    fast_check_prompt = f"Fast-check this about Sri Lanka Election: {prompt}"
    try:
        search_results = tavily_client.search(query=fast_check_prompt, search_depth="basic", max_results=1)
        fast_check_results = []
        for result in search_results['results']:
            if 'title' in result and 'content' in result:
                fast_check_results.append({
                    'title': result['title'],
                    'content': result['content']
                })
        print(fast_check_results)
        return fast_check_results
    except Exception as e:
        return ""

async def combine_responses(response1, response2, tavily_client,prompt):
    is_consistent = check_consistency(response1, response2)
    print(is_consistent)
    aggregated_response = aggregate_responses(response1, response2, is_consistent)
    
    # fast_check_results = await fast_check_response(prompt, tavily_client)
    combine_responses = aggregated_response
    
    # combine_responses = f"query\n{aggregated_response}\n\n Fact-Check Results:\n{fast_check_results}\n\n"
    
    return combine_responses

async def generate_response(prompt, LLAMA_client, tavily_client, general_vector_store, instructions_vector_store, parties_vector_store, memory, type, gemini_model):

    async def get_LLAMA_response(last_prompt):
        try:
            completion = await LLAMA_client.chat.completions.create(
                model="meta/llama-3.1-70b-instruct",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that provides information about Sri Lankan Elections 2024."},
                    {"role": "user", "content": last_prompt}
                ],
                temperature=0.5,
                top_p=0.7,
                max_tokens=2048
            )
            return completion.choices[0].message.content
        except AttributeError:
            # Handle the case where the response is a string instead of an object
            return completion
        except Exception as e:
            print(f"Error in get_LLAMA_response: {e}")
            return "An error occurred while processing your request. Try again later."

    async def get_graph_LLAMA_response(last_prompt):
        try:
            completion = await LLAMA_client.chat.completions.create(
                model="meta/llama-3.1-70b-instruct",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that generates JSON objects for plotting graphs."},
                    {"role": "user", "content": last_prompt}
                ],
                temperature=0.5,
                top_p=0.7,
                max_tokens=4096
            )
            return completion.choices[0].message.content
        except AttributeError:
            # Handle the case where the response is a string instead of an object
            return completion
        except Exception as e:
            print(f"Error in get_LLAMA_response: {e}")
            return "An error occurred while processing your request. Try again later."
                
    async def get_gemini_response(last_prompt):
        chat_session = gemini_model.start_chat()
        final_prompt = f""" 
        role: system, content: You are a helpful assistant that provides information about Sri Lankan Elections 2024.
        role: user, content: {last_prompt}"""
        response = chat_session.send_message(final_prompt)
        return response.text

    if not is_election_or_politics_related(prompt):
        print("Not election or politics related")
        history = memory.load_memory_variables({})
        history_context = "\n".join([f"{m.type}: {m.content}" for m in history.get("history", [])])
        new_prompt = f"Conversation History:\n{history_context}\n\prompt: {prompt}\n\n"
        response = await get_gemini_response(new_prompt)
    
        if type == 1:
            memory.save_context({"input": prompt}, {"output": response})
        Agent = "normal"
        return response , Agent
    else:
        Agent = Agent_selector(prompt)
        if Agent == "general":
            context = await retrieve_context(prompt, general_vector_store)  
        elif Agent == "instructor":
            context = await retrieve_context(prompt, instructions_vector_store)  
        elif Agent == "history":
            context = await retrieve_context(prompt, parties_vector_store)  
        elif Agent == "graph":
            context = await retrieve_context(prompt, general_vector_store,15)
            print(context)
            graph_prompt = f"""Generate a JSON object for plotting a graph related to election data based on this prompt: {prompt}
            Instructions:
            - The response should be a string that can be directly converted into a JSON object in Python.
            - Ensure the accuracy of the information used to plot the graph.(specially the year of the data)
            - Include 'type' (e.g., 'bar', 'line', 'pie', 'stacked_bar', 'bar_demographic') and 'data' keys in the JSON object.
            - 'data' should contain the necessary information for plotting (e.g., labels, values).
            - Do not include any explanatory text outside the JSON structure.
            - Ensure the JSON is valid and can be parsed without additional processing.

            Informations: {context}
            Consider only the information related to the prompt: {prompt}

            JSON Structure:
            {{
                "type": "chart_type",
                "data": {{
                    "labels": ["label1", "label2", ...],
                    "datasets": [
                        {{
                            "label": "Dataset 1",
                            "data": [value1, value2, ...],
                            "backgroundColor": "color1"
                        }},
                        {{
                            "label": "Dataset 2",
                            "data": [value1, value2, ...],
                            "backgroundColor": "color2"
                        }}
                    ]
                }},
                "options": {{
                    "title": {{
                        "text": "Chart Title"
                    }}
                }}
            }}
            remember when giving a line graph the labels should be in order from left to right.(If they are dates it should be in ascending order)
            example:
            {{
                "type": "line",
                "data": {{
                    "labels": ["March 2023", "April 2023", "May 2023", "June 2023", "July 2023", "August 2023", "September 2023", "October 2023", "November 2023", "December 2023", "January 2024", "February 2024", "March 2024", "April 2024", "May 2024", "June 2024", "July 2024", "August 2024"],
            """
            graph_response = await get_graph_LLAMA_response(graph_prompt)
            return graph_response , Agent

    if type == 1:
        history = memory.load_memory_variables({})
        history_context = "\n".join([f"{m.type}: {m.content}" for m in history.get("history", [])])
        context = f"Conversation History:\n{history_context}\n\nContext: {context}\n\n"
        try:
            search_prompt = f"Fast-check this about Sri Lanka Election: {prompt}"
            tavily_context = tavily_client.search(query=search_prompt, search_depth="advanced", max_results=5)
            tavily_context_results = []
            for result in tavily_context['results']:
                if 'title' in result and 'content' in result:
                    tavily_context_results.append({
                        'title': result['title'],
                        'content': result['content']
                    })
            print(tavily_context_results)
            context += f"Additional Context: {tavily_context_results}\n\n"
            # print(context)
        except Exception as e:
            print(f"Error fetching Tavily context: {e}")
            context += "Additional Context: No additional context available.\n\n"

    full_prompt = f"""
    Question: {prompt}

    Instructions:
        Use the provided Context and Additional Context to inform your response.

    {context}

    Answer:
    """
    
    if type == 1:
        LLAMA_response_task = asyncio.create_task(get_LLAMA_response(full_prompt))
        gemini_response_task = asyncio.create_task(get_gemini_response(full_prompt))
        
        LLAMA_response, gemini_response = await asyncio.gather(
            LLAMA_response_task,
            gemini_response_task
        )
        print("LLAMA_response")
        print(LLAMA_response)
        print("gemini_response")
        print(gemini_response)

        combined_response = await combine_responses(LLAMA_response, gemini_response, tavily_client,prompt)
        print(combined_response)
        final_prompt = f"""You are an AI assistant tasked with validating and summarizing information about the Sri Lankan Elections 2024. Please review the following aggregated response, fact-check the information, and provide a concise, accurate summary that a user would find informative and easy to understand.
        If the fact-check results are not available, please provide a summary of the information.
        Here's an aggregated response about the Sri Lankan Elections 2024. Please validate this information, correct any inaccuracies, and present a clear, factual summary for the end user:
        If there was any inacurate information don't tell about it in the summary only add the correct information.
        Make sure you Don't add any markdown syntax to the topic of the response(It is a must)
        {combined_response}"""
        
        final_response = await get_gemini_response(final_prompt)
        memory.save_context({"input": prompt}, {"output": final_response})
        return final_response , Agent    
    else:
        return await get_gemini_response(full_prompt) , Agent

def is_election_or_politics_related(prompt):
    # Creating SLM client
    SLM = InferenceClient(
        "mistralai/Mistral-7B-Instruct-v0.1",
        token=os.getenv("HuggingFace_API_KEY"),
    )

    SLM_prompt = f"""
    Determine if the following prompt is related to elections or politics in Sri Lanka.
    Respond with 'Yes' if it is, and 'No' if it is not.
    
    Prompt: {prompt}
    """
        
    start_time = time.time()
        
    SLM_response = ""
    for message in SLM.chat_completion(
            messages=[{"role": "user", "content": SLM_prompt}],
            max_tokens=10,
            stream=True,
        ):
            content = message.choices[0].delta.content
            if content:
                print(content, end="")
                SLM_response += content 

    end_time = time.time()
    process_time = end_time - start_time
        
    print(f"\nSLM_response: {SLM_response.strip()}")
    print(f"Process time: {process_time:.2f} seconds")

    if "yes" in SLM_response.strip().lower() or "election" in prompt.strip().lower() or "graph" in prompt.strip().lower() or "chart" in prompt.strip().lower() or "plot" in prompt.strip().lower():
        return True
    elif "no" in SLM_response.strip().lower():
        return False
    else:
        print(f"Unexpected SLM response: {SLM_response}")
        return False

def Agent_selector(prompt):
    # Creating SLM client
    SLM = InferenceClient(
        "mistralai/Mistral-7B-Instruct-v0.1",
        token=os.getenv("HuggingFace_API_KEY"),
    )

    SLM_prompt = f"""
    Analyze the following prompt and categorize it based on these criteria:
    1. If it's about drawing a graph or chart related to election data, respond with 'graph'.
    2. If it's about election instructions and guidelines, respond with 'instructor'.
    3. If it's about election history and political parties, respond with 'history'.
    4. If it's related to elections or politics in Sri Lanka but doesn't fit the above categories, respond with 'general'.
    
    Prompt: {prompt}
    """
        
    start_time = time.time()
        
    SLM_response = ""
    for message in SLM.chat_completion(
            messages=[{"role": "user", "content": SLM_prompt}],
            max_tokens=50,
            stream=True,
        ):
            content = message.choices[0].delta.content
            if content:
                print(content, end="")
                SLM_response += content 

    end_time = time.time()
    process_time = end_time - start_time
        
    print(f"\nSLM_response: {SLM_response.strip()}")
    print(f"Process time: {process_time:.2f} seconds")

    response = SLM_response.strip().lower()
    if "graph" in response or "chart" in prompt.strip().lower() or "plot" in prompt.strip().lower():
        return "graph"
    elif "history" in response:
        return "history"
    elif "instructor" in response:
        return "instructor"
    elif "general" in response:
        return "general"
    else:
        print(f"Unexpected SLM response: {SLM_response}")
        return "general"