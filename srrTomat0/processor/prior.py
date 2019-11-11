from srrTomat0.processor.gtf import GTF_GENENAME, GTF_CHROMOSOME, SEQ_START, SEQ_STOP
from srrTomat0.processor.motif_locations import MotifLocationManager as MotifLM

import pybedtools as pbt
from scipy.stats import poisson
from statsmodels.sandbox.stats.multicomp import multipletests

import pandas as pd
import math
from pandas.errors import EmptyDataError
import numpy as np
import multiprocessing

PRIOR_TF = 'regulator'
PRIOR_GENE = 'target'
PRIOR_COUNT = 'count'
PRIOR_SCORE = 'score'
PRIOR_PVAL = 'pvalue'

PRIOR_COLS = [PRIOR_TF, PRIOR_GENE, PRIOR_COUNT, PRIOR_SCORE, PRIOR_PVAL]

PRIOR_FDR = 'qvalue'
PRIOR_SIG = 'significance'


def build_prior_from_atac_motifs(genes, open_chromatin, motif_peaks, num_cores=1, alpha=0.05,
                                 enforced_sparsity_ratio=0.05, multiple_test_correction=False):
    """
    Construct a prior [G x K] interaction matrix
    :param genes: pd.DataFrame [G x n]
    :param open_chromatin: pd.DataFrame
        ATAC peaks loaded from a BED file
    :param motif_peaks: pd.DataFrame
        Motif search data loaded from FIMO or HOMER
    :param num_cores: int
        Number of local cores to use
    :param alpha: float
        alpha value for significance
    :return prior_data, prior_matrix: pd.DataFrame [G*K x 6], pd.DataFrame [G x K]
        A long-form edge table data frame and a wide-form interaction matrix data frame
    """

    motif_names = MotifLM.get_motif_names()
    print("Building prior from {g} genes and {k} TFs".format(g=genes.shape[0], k=len(motif_names)))

    prior_data = []

    if num_cores != 1:
        with multiprocessing.Pool(num_cores, maxtasksperchild=1000) as mp:
            for priors in mp.imap_unordered(_build_prior_for_gene, _gene_generator(genes,
                                                                                   open_chromatin,
                                                                                   motif_peaks)):
                prior_data.append(priors)
    else:
         prior_data = list(map(_build_prior_for_gene, _gene_generator(genes, open_chromatin, motif_peaks)))

    # Combine priors for all genes
    prior_data = pd.concat(prior_data)

    # Pivot to a matrix, extend to all TFs, and fill with 1s
    prior_matrix = prior_data.pivot(index=PRIOR_GENE, columns=PRIOR_TF, values=PRIOR_PVAL)
    prior_matrix = prior_matrix.reindex(motif_names, axis=1)
    prior_matrix = prior_matrix.reindex(genes[GTF_GENENAME], axis=0)
    prior_matrix[pd.isnull(prior_matrix)] = 1

    print("Processing p-values [alpha = {a}] by TF".format(a=alpha))
    # Recalculate a qvalue by FDR (BH)
    for tf in prior_matrix.columns:
        # FDR Correction
        if multiple_test_correction:
            qvals = multipletests(prior_matrix[tf], alpha=alpha, method='fdr_bh')[1]
        else:
            qvals = prior_matrix[tf]

        # Enforce sparsity
        if enforced_sparsity_ratio is not None and enforced_sparsity_ratio < 1:
            max_kept = math.ceil(enforced_sparsity_ratio * len(qvals))
            max_kept_value = qvals[np.argsort(qvals)[max_kept]]
            qvals[qvals > max_kept_value] = 1

        prior_matrix[tf] = qvals

    prior_matrix = prior_matrix < alpha

    return prior_data, prior_matrix


def _gene_generator(genes, open_chromatin, motif_data):
    """

    :param genes:
    :param open_chromatin:
    :param motif_data:
    :yield: str, pd.DataFrame, pd.DataFrame
    """

    for i, (idx, gene_data) in enumerate(genes.iterrows()):

        gene_name = gene_data[GTF_GENENAME]
        gene_chr, gene_start, gene_stop = gene_data[GTF_CHROMOSOME], gene_data[SEQ_START], gene_data[SEQ_STOP]

        chromatin_mask = open_chromatin[GTF_CHROMOSOME] == gene_chr
        chromatin_mask &= open_chromatin[SEQ_STOP] >= gene_start
        chromatin_mask &= open_chromatin[SEQ_START] <= gene_stop

        motif_mask = motif_data[MotifLM.chromosome_col] == gene_chr
        motif_mask &= motif_data[MotifLM.stop_col] >= gene_start
        motif_mask &= motif_data[MotifLM.start_col] <= gene_stop

        yield (gene_name, open_chromatin.loc[chromatin_mask, :], motif_data.loc[motif_mask, :], i)


def _build_prior_for_gene(gene_data):
    """
    Takes ATAC peaks and Motif locations near a single gene and turns them into TF-gene scores

    :param gene_data: (str, pd.DataFrame, pd.DataFrame, int)
        Unpacks to gene_name, chromatin_data, motif_data
        gene_name: str identifier for the gene
        chromatin_data: pd.DataFrame which has the ATAC (open chromatin) peaks near the gene
        motif_data: pd.DataFrame which has the Motif locations near the gene
        num_iteration: int the number of genes which have been processed
    :return prior_edges: pd.DataFrame [N x 5]
        'regulator': tf name
        'target': gene name
        'count': number of motifs found
        'score': negative log10 of p-value
        'pvalue': p-value calculated using poisson survival function
    """

    gene_name, chromatin_data, motif_data, num_iteration = gene_data

    if num_iteration % 100 == 0:
        print("Processing gene {i} [{gn}]".format(i=num_iteration, gn=gene_name))

    if min(chromatin_data.shape) == 0 or min(motif_data.shape) == 0:
        return pd.DataFrame(columns=PRIOR_COLS)

    open_chromatin_peaks = pbt.BedTool.from_dataframe(chromatin_data)

    try:
        open_regulator_peaks = pbt.BedTool.from_dataframe(motif_data)
        open_regulator_peaks = open_regulator_peaks.intersect(open_chromatin_peaks, u=True).to_dataframe()
    except EmptyDataError:
        return pd.DataFrame(columns=PRIOR_COLS)

    open_regulator_peaks.columns = motif_data.columns

    prior_edges = []
    for tf, tf_peaks in open_regulator_peaks.groupby(MotifLM.name_col):
        tf_counts = tf_peaks.shape[0]

        pvals = tf_peaks[MotifLM.score_col]

        # Calculate a score from pvalues
        score = -np.log10(pvals).sort_values(ascending=False).divide([1 / (2 ** n) for n in range(len(pvals))]).sum()

        # Add this edge to the table
        prior_edges.append((tf, gene_name, tf_counts, score, 10 ** (-1 * score)))

    return pd.DataFrame(prior_edges, columns=PRIOR_COLS)