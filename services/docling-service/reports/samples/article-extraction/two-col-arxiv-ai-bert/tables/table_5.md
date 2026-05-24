Table 5: Ablation over the pre-training tasks using the BERTBASE architecture. 'No NSP' is trained without the next sentence prediction task. 'LTR &amp; No NSP' is trained as a left-to-right LM without the next sentence prediction, like OpenAI GPT. '+ BiLSTM' adds a randomly initialized BiLSTM on top of the 'LTR + No NSP' model during fine-tuning.

|             | Dev Set      | Dev Set    | Dev Set    | Dev Set     | Dev Set    |
|-------------|--------------|------------|------------|-------------|------------|
| Tasks       | MNLI-m (Acc) | QNLI (Acc) | MRPC (Acc) | SST-2 (Acc) | SQuAD (F1) |
| BERT BASE   | 84.4         | 88.4       | 86.7       | 92.7        | 88.5       |
| No NSP      | 83.9         | 84.9       | 86.5       | 92.6        | 87.9       |
| LTR &No NSP | 82.1         | 84.3       | 77.5       | 92.1        | 77.8       |
| + BiLSTM    | 82.1         | 84.1       | 75.7       | 91.6        | 84.9       |