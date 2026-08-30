from pathlib import Path


def load_prompt(filename: str) -> str:
    """
    Loads prompt template contents from the prompts directory.
    Raises FileNotFoundError if the prompt file does not exist.
    """
    prompt_path = Path(__file__).parent / filename
    if not prompt_path.is_file():
        raise FileNotFoundError(
            f"Prompt file '{filename}' not found at: {prompt_path.resolve()}"
        )

    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read().strip()
