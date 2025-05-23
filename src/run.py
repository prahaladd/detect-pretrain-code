import logging
logging.basicConfig(level='ERROR')
import numpy as np
from pathlib import Path
import openai
import torch
import zlib
from transformers import AutoTokenizer, AutoModelForCausalLM
from tqdm import tqdm
import numpy as np
from datasets import load_dataset
from options import Options
from eval import *
import os
from openai import OpenAI


def load_model(name1, name2):
    if "davinci" in name1:
        model1 = None
        tokenizer1 = None
    else:
        model1 = AutoModelForCausalLM.from_pretrained(name1, return_dict=True, device_map='auto')
        model1.eval()
        tokenizer1 = AutoTokenizer.from_pretrained(name1)

    if "davinci" in name2:
        model2 = None
        tokenizer2 = None
    else:
        model2 = AutoModelForCausalLM.from_pretrained(name2, return_dict=True, device_map='auto')
        model2.eval()
        tokenizer2 = AutoTokenizer.from_pretrained(name2)
    return model1, model2, tokenizer1, tokenizer2

def calculatePerplexity_gpt3(prompt, modelname):
    prompt = prompt.replace('\x00','')
    responses = None
    # Put your API key here
    api_key = os.environ.get("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key)
    # Map old model names to new ones
    model_mapping = {
        "text-davinci-003": "gpt-3.5-turbo-instruct",
        "text-davinci-002": "gpt-3.5-turbo-instruct"
    }
    modelname = model_mapping.get(modelname, modelname)
    retryCount = 0
    while responses is None and retryCount < 3:
        try:
            responses = client.completions.create(
                        model=modelname, 
                        prompt=prompt,
                        max_tokens=1,  # We need at least 1 token to get logprobs
                        temperature=1.0,
                        logprobs=5,
                        echo=False)
        except openai.BadRequestError as e:
            print(f"OpenAI API Error: {str(e)}")
            if "maximum context length" in str(e).lower():
                print("The input text is too long for the model's context window. Consider using a shorter text or a model with a larger context window.")
            elif "logprobs" in str(e).lower():
                print("The logprobs parameter is not supported or exceeds the maximum value of 5.")
            else:
                print("Please check the OpenAI API documentation for more details about the error.")
            retryCount += 1
    data = responses.choices[0].logprobs
    all_prob = [d for d in data.token_logprobs if d is not None]
    p1 = np.exp(-np.mean(all_prob))
    return p1, all_prob, np.mean(all_prob)

     
def calculatePerplexity(sentence, model, tokenizer, gpu):
    """
    exp(loss)
    """
    # Set the default device to mps
    torch.set_default_device("mps")
    input_ids = torch.tensor(tokenizer.encode(sentence)).unsqueeze(0)
    input_ids = input_ids.to("mps")
    with torch.no_grad():
        outputs = model(input_ids, labels=input_ids)
    loss, logits = outputs[:2]
    
    '''
    extract logits:
    '''
    # Apply softmax to the logits to get probabilities
    probabilities = torch.nn.functional.log_softmax(logits, dim=-1)
    # probabilities = torch.nn.functional.softmax(logits, dim=-1)
    all_prob = []
    input_ids_processed = input_ids[0][1:]
    for i, token_id in enumerate(input_ids_processed):
        probability = probabilities[0, i, token_id].item()
        all_prob.append(probability)
    return torch.exp(loss).item(), all_prob, loss.item()


def inference(model1, model2, tokenizer1, tokenizer2, text, ex, modelname1, modelname2):
    pred = {}

    if "davinci" in modelname1:
        p1, all_prob, p1_likelihood = calculatePerplexity_gpt3(text, modelname1) 
        p_lower, _, p_lower_likelihood = calculatePerplexity_gpt3(text.lower(), modelname1)
    else:
        p1, all_prob, p1_likelihood = calculatePerplexity(text, model1, tokenizer1, gpu=model1.device)
        p_lower, _, p_lower_likelihood = calculatePerplexity(text.lower(), model1, tokenizer1, gpu=model1.device)

    if "davinci" in modelname2:
        p_ref, all_prob_ref, p_ref_likelihood = calculatePerplexity_gpt3(text, modelname2)
    else:
        p_ref, all_prob_ref, p_ref_likelihood = calculatePerplexity(text, model2, tokenizer2, gpu=model2.device)
   
   # ppl
    pred["ppl"] = p1
    # Ratio of log ppl of large and small models
    pred["ppl/Ref_ppl (calibrate PPL to the reference model)"] = p1_likelihood-p_ref_likelihood


    # Ratio of log ppl of lower-case and normal-case
    pred["ppl/lowercase_ppl"] = -(np.log(p_lower) / np.log(p1)).item()
    # Ratio of log ppl of large and zlib
    zlib_entropy = len(zlib.compress(bytes(text, 'utf-8')))
    pred["ppl/zlib"] = np.log(p1)/zlib_entropy
    # min-k prob
    for ratio in [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]:
        k_length = int(len(all_prob)*ratio)
        topk_prob = np.sort(all_prob)[:k_length]
        pred[f"Min_{ratio*100}% Prob"] = -np.mean(topk_prob).item()

    ex["pred"] = pred
    return ex

def evaluate_data(test_data, model1, model2, tokenizer1, tokenizer2, col_name, modelname1, modelname2):
    print(f"all data size: {len(test_data)}")
    all_output = []
    test_data = test_data
    for ex in tqdm(test_data): 
        text = ex[col_name]
        new_ex = inference(model1, model2, tokenizer1, tokenizer2, text, ex, modelname1, modelname2)
        all_output.append(new_ex)
    return all_output


if __name__ == '__main__':
    args = Options()
    args = args.parser.parse_args()
    args.output_dir = f"{args.output_dir}/{args.target_model}_{args.ref_model}/{args.key_name}"
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    # load model and data
    model1, model2, tokenizer1, tokenizer2 = load_model(args.target_model, args.ref_model)
    if "jsonl" in args.data:
        data = load_jsonl(f"{args.data}")
    else: # load data from huggingface
        dataset = load_dataset(args.data, split=f"WikiMIA_length{args.length}")
        data = convert_huggingface_data_to_list_dic(dataset)

    all_output = evaluate_data(data, model1, model2, tokenizer1, tokenizer2, args.key_name, args.target_model, args.ref_model)
    fig_fpr_tpr(all_output, args.output_dir)

