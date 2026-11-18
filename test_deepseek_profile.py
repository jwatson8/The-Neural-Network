#!/usr/bin/env python3
"""Standalone test script for DeepSeek movie-preference profile generation."""

import os
import sys
from openai import OpenAI

from src.service.profile_worker import generate_profile

# Test cases covering success scenarios and known failure modes
test_cases = [
    {
        "name": "both_clear",
        "description": "Both likes and dislikes present",
        "likes": "Classic science fiction from the 1980s",
        "dislikes": "Gore and violence",
    },
    {
        "name": "likes_only",
        "description": "Only likes present (failed in prior tests)",
        "likes": "Romantic comedies and drama",
        "dislikes": "",
    },
    {
        "name": "qualified_dislikes",
        "description": "Qualified dislikes (failed in prior tests)",
        "likes": "Action movies",
        "dislikes": "Horror, but not psychological thrillers",
    },
    {
        "name": "minimal",
        "description": "Very short preferences",
        "likes": "Sci-fi",
        "dislikes": "Horror",
    },
    {
        "name": "empty",
        "description": "Empty preferences (should short-circuit)",
        "likes": "",
        "dislikes": "",
    },
]

def test_profile_generation():
    """Test DeepSeek profile generation with diagnostics."""
    
    # Validate environment
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY not set")
        sys.exit(1)
    
    base_url = os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://spark-60c4.tailcead09.ts.net:10000/v1",
    )
    model_name = os.getenv("DEEPSEEK_MODEL", "deepseek-r1:1.5b")
    timeout = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "300"))
    
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
    )
    
    print(f"DeepSeek Profile Generation Test")
    print(f"================================")
    print(f"Base URL: {base_url}")
    print(f"Model: {model_name}")
    print(f"Timeout: {timeout}s")
    print(f"API Key: {api_key[:10]}...{api_key[-5:]}")
    print()
    
    results = {"passed": 0, "failed": 0, "skipped": 0}
    
    for test_case in test_cases:
        name = test_case["name"]
        desc = test_case["description"]
        likes = test_case["likes"]
        dislikes = test_case["dislikes"]
        
        print(f"Test: {name}")
        print(f"  Description: {desc}")
        print(f"  Likes: {likes!r}")
        print(f"  Dislikes: {dislikes!r}")
        
        # Short-circuit empty case
        if not likes.strip() and not dislikes.strip():
            print(f"  Result: SKIPPED (empty preferences)\n")
            results["skipped"] += 1
            continue
        
        try:
            profile, usage = generate_profile(client, model_name, likes, dislikes)
            input_tokens = getattr(usage, "input_tokens", None) or getattr(
                usage, "prompt_tokens", 0
            )
            output_tokens = getattr(usage, "output_tokens", None) or getattr(
                usage, "completion_tokens", 0
            )
            print(f"  Tokens - Input: {input_tokens}, Output: {output_tokens}")
            print(f"  Positive: {profile.positive!r}")
            print(f"  Negative: {profile.negative!r}")
            
            # Check for invented content
            invented = False
            if profile.positive and likes and len(profile.positive) > len(likes) * 3:
                # Heuristic: if output is > 3x input, check for likely additions
                print(f"  ⚠ Warning: positive output is suspiciously long")
                invented = True
            if profile.negative and dislikes and len(profile.negative) > len(dislikes) * 3:
                print(f"  ⚠ Warning: negative output is suspiciously long")
                invented = True
            
            if invented:
                print(f"  Result: PASSED (but check for invented content)\n")
            else:
                print(f"  Result: PASSED\n")
            results["passed"] += 1
        
        except Exception as e:
            print(f"  Error: {type(e).__name__}")
            print(f"  Details: {e}")
            print(f"  Result: FAILED\n")
            results["failed"] += 1
    
    # Summary
    print(f"Summary")
    print(f"=======")
    print(f"Passed: {results['passed']}")
    print(f"Failed: {results['failed']}")
    print(f"Skipped: {results['skipped']}")
    
    if results["failed"] > 0:
        print(f"\n⚠ {results['failed']} test(s) failed. See diagnostics above.")
        sys.exit(1)
    else:
        print(f"\n✓ All tests passed!")
        sys.exit(0)

if __name__ == "__main__":
    test_profile_generation()
