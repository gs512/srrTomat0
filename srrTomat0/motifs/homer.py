import subprocess
import io
import pandas as pd

from srrTomat0.motifs import chunk_motifs, homer_motif, SCAN_SCORE_COL
from srrTomat0.motifs._motif import __MotifScanner
from srrTomat0 import HOMER_EXECUTABLE_PATH

HOMER_DATA_SUFFIX = ".homer.tsv"

HOMER_SEQ_ID = 'seqid'
HOMER_OFFSET = 'offset'
HOMER_MATCH = 'match'
HOMER_MOTIF = 'motif_id'
HOMER_STRAND = 'strand'
HOMER_SCORE = 'score'
HOMER_CHROMOSOME = 'sequence_name'
HOMER_START = 'start'
HOMER_STOP = 'stop'

HOMER2_FIND_COLS = [HOMER_SEQ_ID, HOMER_OFFSET, HOMER_MATCH, HOMER_MOTIF, HOMER_STRAND, HOMER_SCORE]


class HOMERScanner(__MotifScanner):

    def _preprocess(self, min_ic=None):
        if self.motif_file is not None:
            self.motifs = homer_motif.read(self.motif_file)

        return chunk_motifs(homer_motif, self.motifs, num_workers=self.num_workers, min_ic=min_ic)

    def _postprocess(self, motif_peaks):
        motif_peaks = motif_peaks.drop_duplicates(subset=[HOMER_MOTIF, HOMER_START, HOMER_STOP, HOMER_CHROMOSOME])
        return motif_peaks

    def _get_motifs(self, fasta_file, motif_file):
        homer_command = [HOMER_EXECUTABLE_PATH, "find", "-i", fasta_file, "-m", motif_file, "-offset", str(0)]
        proc = subprocess.run(homer_command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        if int(proc.returncode) != 0:
            print("HOMER motif scan failed for {meme}, {fa} (cmd)".format(meme=motif_file,
                                                                          fa=fasta_file,
                                                                          cmd=" ".join(homer_command)))

        return self._parse_output(io.StringIO(proc.stdout.decode("utf-8")))

    def _parse_output(self, output_handle):
        motifs = pd.read_csv(output_handle, sep="\t", index_col=None, names=HOMER2_FIND_COLS)

        loc_data = motifs[HOMER_SEQ_ID].str.split(r"[\:\-]", expand=True)
        loc_data.columns = [HOMER_CHROMOSOME, HOMER_START, HOMER_STOP, "UNK"]
        loc_data[HOMER_START] = loc_data[HOMER_START].astype(int) + motifs[HOMER_OFFSET]

        match_width = motifs[HOMER_MATCH].str.len()

        loc_data.loc[motifs[HOMER_STRAND] == "-", HOMER_START] -= match_width.loc[motifs[HOMER_STRAND] == "-"] - 1

        loc_data[HOMER_STOP] = loc_data[HOMER_START] + motifs[HOMER_MATCH].str.len()

        motifs[[HOMER_CHROMOSOME, HOMER_START, HOMER_STOP]] = loc_data[[HOMER_CHROMOSOME, HOMER_START, HOMER_STOP]]
        motifs.drop([HOMER_SEQ_ID, HOMER_OFFSET], inplace=True, axis=1)

        motifs[SCAN_SCORE_COL] = [self.motifs[x].score_match(y) for x, y in
                                  zip(motifs[HOMER_MOTIF], motifs[HOMER_MATCH])]

        return motifs
