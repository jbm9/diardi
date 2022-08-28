#!/usr/bin/env python3
"""Sends spectral and A-weighted sound readings to influxdb"""

import argparse
import datetime
import logging
import platform
import time

from influxdb import InfluxDBClient
import numpy as np
import sounddevice as sd

VERSION = '0.0.1'
DEFAULT_BUCKET = f"soundlevel_v{VERSION}"

TAGS = {"version": VERSION, "sensorId": 0}

def int_or_str(text):
    """Helper function for argument parsing."""
    try:
        return int(text)
    except ValueError:
        return text


# The following slightly weird parser config is stolen from a
# sounddevice library example program

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('-l', '--list-devices', action='store_true',
                    help='show list of audio devices and exit')

args, remaining = parser.parse_known_args()

if args.list_devices:
    print(sd.query_devices())
    parser.exit(0)

parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[parser])

parser.add_argument('-d', '--device', type=int_or_str,
                    help='input device (numeric ID or substring)')

parser.add_argument('-b', '--blocksize', type=int, metavar='NSAMPLES', default=1<<20,
                    help='block size (default %(default)s samples)')

parser.add_argument("--server", default="127.0.0.1", metavar="HOSTNAME",
                    help="Influxdb hostname string to use (default: %(default)s)")
parser.add_argument("--database", default=DEFAULT_BUCKET,
                    help=f"Influxdb database to use (default: %(default)s)")
parser.add_argument("--measurement-name", help="Name of measurement (default: %(default)s)", default="fftmag")

parser.add_argument("--nodename", default=platform.node(),
                    help="Hostname to use when submitting data (default: %(default)s)")

args = parser.parse_args(remaining)
samplerate = 48000

fftsize = 2048

TAGS["nodename"] = args.nodename


client = InfluxDBClient(host=args.server)
client.create_database(args.database)
client.switch_database(args.database)

client.write_points([{
            "measurement": "startup",
            "fields": { "i": 1, },
            "time": datetime.datetime.utcnow().isoformat(),
            }])


def callback(indata, frames, time, status):
    # A handy A-weighting IIR filter for 48kHz by Matt L, from:
    # https://dsp.stackexchange.com/questions/36077/design-of-a-digital-a-weighting-filter-with-arbitrary-sample-rate

    b =[ 0.169994948147430,
         0.280415310498794,
         -1.120574766348363,
         0.131562559965936,
         0.974153561246036,
         -0.282740857326553,
         -0.152810756202003,]


    a = [ 1.00000000000000000,
          -2.12979364760736134,
          0.42996125885751674,
          1.62132698199721426,
          -0.96669962900852902,
          0.00121015844426781,
          0.04400300696788968,
          ]

    ts = datetime.datetime.utcnow().isoformat()
    json_body = []

    client.write_points([{
            "measurement": "foobar",
            "fields": { "i": 1, },
            "time": ts,
            }])

    got_status = False

    if status:
        logging.error(f'Got error status: {status}')
        errtags = TAGS.copy()
        errtags["error"] = "got_status"

        json_body.append({
            "measurement": args.measurement_name + "_error",
            "fields": { "status_text": status, },
            "tags": errtags,
            "time": ts,
            })

        got_status = True

    if any(indata):
        n0 = 0
        hz_per_bin = samplerate/fftsize

        f0 = 0
        f_nyquist = samplerate//2  # integer for formatting

        splits = [ 100, 500, 1000, 2500, 5000, 10000, ]
        mag_sums = [ 0 for _ in splits ]
        mag_cnts = [ 0 for _ in splits ]

        while n0 < indata.shape[0]-fftsize:
            mags = np.abs(np.fft.rfft(indata[n0:n0+fftsize,0], n=fftsize))
            mags *= mags
            n0 += fftsize

            last_bin1 = 1 # skip DC
            for i, f_cut in enumerate(splits):
                if f_cut > f_nyquist:
                    break

                bin0 = last_bin1
                bin1 = min(f_cut//hz_per_bin, fftsize)

                for j in range(int(bin0), int(bin1)):
                    mag_sums[i] += mags[j]
                    mag_cnts[i] += 1

                last_bin1 = bin1


        results = { "rms": np.sqrt(np.sum(mags)/len(mags))}
        last_fcut = 0
        for i, f_cut in enumerate(splits):
            if f_cut > f_nyquist:
                break

            lbl = f"f{last_fcut}-{f_cut}"
            results[lbl] = np.sqrt(mag_sums[i]/mag_cnts[i])
            last_fcut = f_cut

        d = {
            "measurement": args.measurement_name,
            "tags": TAGS,
            "time": ts,
            "fields": results,
        }

        json_body.append(d)



    else:
        logging.warning("No data received in callback")

        # Don't report empty data if we got a status message
        if not got_status:
            errtags = TAGS.copy()
            errtags["error"] = "got_status"

            json_body.append({
                "measurement": args.measurement_name + "_error",
                "fields": { "status_text": "Empty data" },
                "time": ts,
            })

    client.write_points(json_body)

try:
    with sd.InputStream(device=args.device, channels=1, callback=callback,
                        blocksize=args.blocksize, samplerate=samplerate):

        while True:
            time.sleep(1)


except KeyboardInterrupt:
    parser.exit('Interrupted')
except Exception as e:
    parser.exit(type(e).__name__ + ": " + str(e))
