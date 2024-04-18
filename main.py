import os
import sys
import json
import openai
import asyncio
import logging
import aiofiles
import csv
from dotenv import load_dotenv
import atexit
from typing import Any, List, Optional
import logging
import datetime
from tiktoken import get_encoding
import subprocess
import groq
from clients.coze import AsyncCoze


# python3 main.py


# Load environment variables from .env file
load_dotenv()


# CONFIGS: MODEL
OPENAI_MODELS = [
    "gpt-3.5-turbo",
]

OPENAI_JSON_MODE_SUPPORTED_MODELS = [
    "gpt-3.5-turbo-1106",
    "gpt-4-1106-preview",
]

LOCAL_LLM_MODELS = [
    "llama-2-7b-chat.Q8_0.gguf",
]

TOGETHER_AI_MODELS = [
    "togethercomputer/Llama-2-7B-32K-Instruct",
    "mistralai/Mixtral-8x7B-Instruct-v0.1",
]

GROQ_MODELS = [
    "gemma-7b-it",
    "llama2-70b-4096",
    "mixtral-8x7b-32768",
]

# coze bot ids
COZE_BOTS = [
    # "7347642294285795336",  # Kar Wi's Fact Checker Bot
]


# change model here
MODEL_NAME = COZE_BOTS[0]


# CONFIGS: PROMPT
TEXT_DELIMITER = "\n"
KNOWLEDGE_CUTOFF = "April 2023"


# CONFIGS: INPUT PREPROCESSING
MAX_TOKENS = 3000
BATCH_SIZE_IN_TOKENS = int(MAX_TOKENS * 0.7)
MAX_LINES_PER_BATCH = 1  # Maximum number of lines allowed in each batch


# CONFIGS: PATHS
FACT_CHECK_DATASET_FILENAME = "DataSet_Misinfo_first100"
TEST_FILE_PATH = f"test/{FACT_CHECK_DATASET_FILENAME}.orig"
FINAL_OUTPUT_PATH = f"predicted_output/{FACT_CHECK_DATASET_FILENAME}.predicted"
CSV_OUTPUT_PATH = (
    f"predicted_output/{FACT_CHECK_DATASET_FILENAME}.predicted.csv"
)
REFERENCE_ANSWERS_PATH = (
    f"reference_output/{FACT_CHECK_DATASET_FILENAME}.correct"
)


# CONFIGS: API
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT", "")
LOCAL_ENDPOINT = os.getenv("LOCAL_ENDPOINT", "")
TOGETHER_ENDPOINT = os.getenv("TOGETHER_ENDPOINT", "")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
COZE_API_KEY = os.getenv("COZE_API_KEY", "")
MAX_RETRIES = 3  # Maximum number of retries for an API call
RETRY_DELAY = 30  # Delay in seconds before retrying an API
QPM_LIMIT = 10  # Queries per minute limit


# CONFIGS: OTHERS
# ANSI escape codes for colors
RED = "\033[1;31m"
GREEN = "\033[1;32m"
YELLOW = "\033[93m"
BLUE = "\033[1;34m"
RESET = "\033[0m"

INCLUDE_INPUT_IN_CSV = True
INCLUDE_ANSWER_IN_CSV = True


# for coze, please manually define the prompt on the platform
FACT_CHECK_PROMPT = f"""You are a language model trained to evaluate the truthfulness of statements based on your knowledge, which is current up to {KNOWLEDGE_CUTOFF}. Your tasks are to:
1. Read the user-provided statement.
2. Evaluate the statement based on your knowledge up to {KNOWLEDGE_CUTOFF}.
3. Output "SUPPORTS" if the statement is entirely accurate based on your knowledge, "REFUTES" if the statement is entirely inaccurate, or "NOT ENOUGH INFO". Do not provide explanations or additional information.

# Desired format
For example, if the input is:
{{"input": "The tallest building in the world as of {KNOWLEDGE_CUTOFF} is the Burj Khalifa."}}

Your output should be JSON only:
{{"prediction": "SUPPORTS"}}

Another example, if the input is:
{{"input": "As of {KNOWLEDGE_CUTOFF}, the iPhone 12 is the latest model of the iPhone."}}

Your output should be JSON only:
{{"prediction": "NOT ENOUGH INFO"}}

Note: Your evaluations should only be based on factual information available up to {KNOWLEDGE_CUTOFF}."""


# Generate a unique identifier for this run based on the current timestamp
run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Define log file paths with the unique run identifier
LOGGING_OUTPUT_PATH = f"logs/run_{run_id}.log"
ERROR_OUTPUT_PATH = f"logs/error_{run_id}.log"

# Configure logging to output to a file
logging.basicConfig(
    level=logging.INFO,
    format=f"{BLUE}%(asctime)s{RESET} - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOGGING_OUTPUT_PATH),
        logging.StreamHandler(),
    ],
)

# Create a separate handler for error logs
error_handler = logging.FileHandler(ERROR_OUTPUT_PATH)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(
    logging.Formatter(f"{RED}%(asctime)s{RESET} - %(levelname)s - %(message)s")
)

# Get the root logger and add the error handler
root_logger = logging.getLogger()
root_logger.addHandler(error_handler)


# Initialize the OpenAI client based on the selected model
# TODO: return type
def get_openai_client(model_name: str) -> Any:
    if model_name in GROQ_MODELS:
        return groq.AsyncGroq(api_key=GROQ_API_KEY)
    if model_name in LOCAL_LLM_MODELS:
        # Point to the local server
        return openai.AsyncOpenAI(
            base_url=LOCAL_ENDPOINT, api_key="not-needed"
        )
    if model_name in TOGETHER_AI_MODELS:
        # Point to the local server
        return openai.AsyncOpenAI(
            base_url=TOGETHER_ENDPOINT, api_key=TOGETHER_API_KEY
        )
    if model_name in COZE_BOTS:
        return AsyncCoze(api_key=COZE_API_KEY)

    # Initialize the OpenAI client with Azure endpoint and API key
    return openai.AsyncAzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_version="2023-12-01-preview",
        api_key=OPENAI_API_KEY,
    )


client = get_openai_client(MODEL_NAME)


# Rate limiter using an asyncio Semaphore
class RateLimiter:
    def __init__(self, rate_limit: int):
        self.rate_limit = rate_limit
        self.semaphore = asyncio.Semaphore(rate_limit)

    async def __aenter__(self):
        await self.semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await asyncio.sleep(60 / self.rate_limit)
        self.semaphore.release()


rate_limiter = RateLimiter(QPM_LIMIT)


async def main():
    global rate_limiter
    rate_limiter = RateLimiter(
        QPM_LIMIT
    )  # Initialize rate_limiter in the async context
    await process_file(
        client, TEST_FILE_PATH, CSV_OUTPUT_PATH, REFERENCE_ANSWERS_PATH
    )


def format_user_content(text: str) -> str:
    # TODO: better way?
    text_with_next_token = text.replace("\n", TEXT_DELIMITER)
    return json.dumps({"input": text_with_next_token})


def count_tokens(text: str) -> int:
    enc = get_encoding("gpt2")
    tokens = enc.encode(text)
    token_count = len(tokens)
    return token_count


def calculate_avg_chars_per_token(sample_text: str) -> float:
    total_tokens = count_tokens(sample_text)
    total_chars = len(sample_text)
    return total_chars / total_tokens


def split_text_into_batches(
    text: str,
    batch_size_in_tokens: int = BATCH_SIZE_IN_TOKENS,
    max_lines: int = MAX_LINES_PER_BATCH,
) -> List[str]:
    lines = text.split("\n")
    batches = []
    current_batch = ""
    current_batch_tokens = 0
    current_batch_lines = 0

    for line in lines:
        line_tokens = count_tokens(line + "\n")
        if line_tokens > batch_size_in_tokens:
            print(
                f"Error: Line exceeds the batch size of {batch_size_in_tokens} tokens."
            )
            print("Line:", line)
            print("Tokens:", line_tokens)
            sys.exit(1)

        # If max_lines is None or the current batch size and lines are within limits
        if current_batch_tokens + line_tokens <= batch_size_in_tokens and (
            max_lines is None or current_batch_lines < max_lines
        ):
            current_batch += line + "\n"
            current_batch_tokens += line_tokens
            current_batch_lines += 1
        else:
            batches.append(current_batch.strip())
            current_batch = line + "\n"
            current_batch_tokens = line_tokens
            current_batch_lines = 1

    if current_batch.strip():
        batches.append(current_batch.strip())

    return batches


def escape_special_characters(s):
    """Returns a visually identifiable string for special characters."""
    return s.replace("\n", "\\n").replace("\t", "\\t")


def extract_error_snippet(error: json.JSONDecodeError, window=20):
    start = max(
        error.pos - window, 0
    )  # Start a bit before the error, if possible
    end = min(
        error.pos + window, len(error.doc)
    )  # End a bit after the error, if possible

    # Extract the snippet around the error
    snippet_start = error.doc[start : error.pos]
    snippet_error = error.doc[
        error.pos : error.pos + 1
    ]  # The erroneous character
    snippet_end = error.doc[error.pos + 1 : end]

    # Escape special characters in the erroneous part
    snippet_error_escaped = escape_special_characters(snippet_error)

    snippet = f"...{snippet_start}{RED}{snippet_error_escaped}{RESET}{snippet_end}..."
    return snippet


async def ask_llm(
    client: Any,
    prompt: str,
    text: str,
    batch_number: int,
    total_batches: int,
    model_name: str,
) -> str:
    retries = 0
    while retries < MAX_RETRIES:
        try:
            logging.info(
                f"Sending request for batch {batch_number}/{total_batches}: {text}"
            )
            model_params = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": format_user_content(text)},
                ],
                "temperature": 0,
                "max_tokens": MAX_TOKENS,
            }
            if model_name in OPENAI_JSON_MODE_SUPPORTED_MODELS:
                model_params["response_format"] = {"type": "json_object"}
            if model_name in COZE_BOTS:
                # TODO: extract to .env
                model_params = {
                    "bot_id": model_name,
                    "user": "KyleToh",
                    "query": format_user_content(text),
                    "stream": False,
                }

            # TODO: extract to a function
            completion = await client.chat.completions.create(**model_params)
            response = completion.choices[0].message.content

            # TODO: debug special character
            logging.info(
                f"{YELLOW}Received raw response for batch {batch_number}/{total_batches}: {response}{RESET}"
            )
            content_json = json.loads(response)
            response_text = content_json.get("prediction")
            if response_text is None:
                raise ValueError("'text' field not found in response JSON")

            # TODO: extract to a function
            response_lines = []
            for line in response_text.split(TEXT_DELIMITER):
                response_lines.append(line.strip())

            final_text = "\n".join(response_lines)

            assert len(response_lines) == len(
                text.split("\n")
            ), "Number of lines in response_text does not match the number of lines in text."

            return final_text
        except json.JSONDecodeError as e:
            error_snippet = extract_error_snippet(e)
            logging.error(
                f"Error processing response for batch {batch_number}/{total_batches}: {error_snippet}"
            )
        except AssertionError as e:
            logging.error(
                f"Error processing response for batch {batch_number}/{total_batches}: {e}"
            )
        except Exception as e:
            logging.error(
                f"An error occurred while processing batch {batch_number}/{total_batches}: {e}"
            )
        retries += 1
        if retries < MAX_RETRIES:
            logging.info(
                f"{YELLOW}Retrying for batch {batch_number}/{total_batches} (Attempt {retries}/{MAX_RETRIES}){RESET}"
            )
            await asyncio.sleep(RETRY_DELAY)
        else:
            logging.error(
                f"Max retries reached for batch {batch_number}/{total_batches}. Exiting the program."
            )
            sys.exit(1)  # Exit the program with a non-zero status code
    raise RuntimeError("Unexpected execution path")


async def predict_label_and_write_csv(
    client: Any,
    text: str,
    batch_number: int,
    total_batches: int,
    csv_writer: Any,
    model_name: str,
    correct_answer: str,
) -> str:
    async with rate_limiter:
        predicted_label = await ask_llm(
            client,
            FACT_CHECK_PROMPT,
            text,
            batch_number,
            total_batches,
            model_name,
        )

        logging.info(
            f"{GREEN}Received prediction for batch {batch_number}/{total_batches}: {predicted_label}{RESET}"
        )

        # Write the batch number and predicted text to the CSV
        row = {
            "Batch Number": batch_number,
            "Predicted Label": predicted_label,
        }
        if INCLUDE_INPUT_IN_CSV:
            row["Input Text"] = text
        if INCLUDE_ANSWER_IN_CSV:
            row["Correct Label"] = correct_answer

        await csv_writer.writerow(row)
        return predicted_label


# Function to check which batches have already been processed
async def get_processed_batches(csv_output_path: str) -> set[int]:
    processed_batches = set()
    try:
        async with aiofiles.open(csv_output_path, "r") as csv_file:
            content = await csv_file.read()
            reader = csv.DictReader(content.splitlines())
            for row in reader:
                try:
                    batch_number = int(row["Batch Number"])
                    processed_batches.add(batch_number)
                except (ValueError, KeyError):
                    # Skip rows with invalid or missing "Batch Number"
                    continue
    except FileNotFoundError:
        # If the CSV file does not exist, return an empty set
        pass
    return processed_batches


async def process_file(
    client: Any,
    test_file_path: str,
    csv_output_path: str,
    reference_answers_path: Optional[str] = None,
):
    # Check for existing output files
    if os.path.exists(FINAL_OUTPUT_PATH) or os.path.exists(CSV_OUTPUT_PATH):
        user_input = (
            input(
                "Existing output files found. Do you want to continue with existing files? Type 'reset' to delete and start fresh: "
            )
            .strip()
            .lower()
        )

        if user_input == "reset":
            if os.path.exists(FINAL_OUTPUT_PATH):
                os.remove(FINAL_OUTPUT_PATH)
            if os.path.exists(CSV_OUTPUT_PATH):
                os.remove(CSV_OUTPUT_PATH)
            print("Existing files removed. Starting fresh...")
        else:
            print("Continuing with existing files...")

    processed_batches = await get_processed_batches(csv_output_path)
    file_exists = os.path.exists(csv_output_path)
    should_write_header = (
        not file_exists or os.stat(csv_output_path).st_size == 0
    )

    async with aiofiles.open(test_file_path, "r") as test_file:
        text = await test_file.read()

    batches = split_text_into_batches(text, BATCH_SIZE_IN_TOKENS)

    answers_batches = [None] * len(batches)

    # Conditionally load answers if a valid reference_answers_path is provided and exists
    if reference_answers_path and os.path.exists(reference_answers_path):
        async with aiofiles.open(reference_answers_path, "r") as answers_file:
            text_answers = await answers_file.read()
            # Assuming text_answers need to be split according to the number of batches
            answers_batches = text_answers.split("\n", len(batches) - 1)

    async with aiofiles.open(csv_output_path, "a", newline="") as csv_file:
        fieldnames = ["Batch Number"]
        if INCLUDE_INPUT_IN_CSV:
            fieldnames.append("Input Text")
        if INCLUDE_ANSWER_IN_CSV:
            fieldnames.append("Correct Label")
        fieldnames.append("Predicted Label")

        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if should_write_header:
            await csv_file.write(
                ",".join(f'"{name}"' for name in fieldnames) + "\n"
            )

        assert len(batches) == len(
            answers_batches
        ), "Mismatch between number of input batches and answers batches."

        total_batches = len(batches)
        tasks = []
        for batch_number, (batch_text, correct_answer) in enumerate(
            zip(batches, answers_batches), start=1
        ):
            if batch_number in processed_batches:
                continue
            tasks.append(
                predict_label_and_write_csv(
                    client,
                    batch_text,
                    batch_number,
                    total_batches,
                    csv_writer,
                    MODEL_NAME,
                    correct_answer or "",
                )
            )

        await asyncio.gather(*tasks)


def generate_prediction_file_from_csv(csv_output_path: str, output_path: str):
    with open(
        csv_output_path, mode="r", newline="", encoding="utf-8"
    ) as csv_file:
        csv_reader = csv.DictReader(csv_file)
        sorted_rows = sorted(
            csv_reader, key=lambda row: int(row["Batch Number"])
        )

    with open(
        output_path, mode="w", newline="", encoding="utf-8"
    ) as output_file:
        for row in sorted_rows:
            if "Predicted Label" in row:
                predicted_labels = row["Predicted Label"].split("\n")
                for predicted_label in predicted_labels:
                    output_file.write(predicted_label + "\n")


# Function to log a divider when the program exits
def log_exit_divider():
    logging.info("=" * 80)


# Register the exit function
atexit.register(log_exit_divider)


def prompt_for_evaluation():
    user_response = (
        input("Do you want to evaluate the result? (yes/no): ").strip().lower()
    )
    if user_response == "yes":
        try:
            # Execute the evaluation script
            print("Evaluating the predictions...")
            # TODO: replace
            subprocess.run(
                ["python3", "commands/evaluate_model_performance.py"],
                check=True,
            )
            print("Evaluation completed successfully.")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred during evaluation: {e}")
    elif user_response == "no":
        print("Evaluation skipped.")
    else:
        print("Invalid input. Please type 'yes' or 'no'.")
        prompt_for_evaluation()


if __name__ == "__main__":
    logging.info("=" * 80)
    logging.info(f"Model selected: {MODEL_NAME}")
    logging.info(
        f"{BLUE}Using prompt: {escape_special_characters(FACT_CHECK_PROMPT)}{RESET}"
    )
    logging.info("Starting to process the file...")
    asyncio.run(main())
    logging.info("Generating the predicted file from CSV...")
    generate_prediction_file_from_csv(CSV_OUTPUT_PATH, FINAL_OUTPUT_PATH)
    logging.info("File processing completed.")
    prompt_for_evaluation()
    logging.info("=" * 80)
