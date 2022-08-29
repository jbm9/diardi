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

VERSION = '0.0.2'
DEFAULT_BUCKET = f"soundlevel_v{VERSION}"

TAGS = {"version": VERSION, "sensorId": 0}


############################################################
# Arg parsing

def int_or_str(text):
    """Helper function for argument parsing."""
    try:
        return int(text)
    except ValueError:
        return text


parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)

parser.add_argument('-l', '--list-devices', action='store_true',
                    help='show list of audio devices and exit')

parser.add_argument("--debug", action="store_true",
                    help="Enable debug logging")

parser.add_argument('-d', '--device', type=int_or_str,
                    help='input device (numeric ID or substring)')

parser.add_argument('-b', '--blocksize', type=int, metavar='NSAMPLES',
                    default=1<<(11+3),
                    help='block size (default: %(default)s samples)')

parser.add_argument("-t", "--time", type=float, metavar="SECONDS",
                    default=30,
                    help="Time between sample transmissions (default: %(default)s seconds)")

parser.add_argument("--server", default="127.0.0.1", metavar="HOSTNAME",
                    help="Influxdb hostname string to use (default: %(default)s)")

parser.add_argument("--database", default=DEFAULT_BUCKET,
                    help=f"Influxdb database to use (default: %(default)s)")

parser.add_argument("--measurement-name", default="fftmag",
                    help="Name of measurement (default: %(default)s)")

parser.add_argument("--nodename", default=platform.node(),
                    help="Hostname to use when submitting data (default: %(default)s)")

args = parser.parse_args()

if args.debug:
    logging.basicConfig(level=logging.DEBUG)

logging.debug(f'Got args: {args}')

if args.list_devices:
    print(sd.query_devices())
    parser.exit(0)

############################################################
# Actual program

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


####################
# Global shared: note that this confines us to single-threading without a mutex
bin_accumulator = np.zeros(fftsize)  # Accumulate squares of bins
n_samples = 0  # Count of FFT samples we've taken

# A queue of readings to kick over to the server, used to queue up
# status messages for OOB sending.
frame_queue = []

def callback(indata, frames, time, status):
    '''Callback for sounddevice after it's accumulated our frames

    This just accumulates state into the above globals, with all
    influx work happening in the main loop below.
    '''
    global bin_accumulator, n_samples, frame_queue

    ts = datetime.datetime.utcnow().isoformat()

    #    logging.debug(f'Got samples {len(indata)}')
    got_status = False

    if status:
        logging.error(f'Got error status: {status}')
        errtags = TAGS.copy()
        errtags["error"] = "got_status"

        frame_queue.append({
            "measurement": args.measurement_name + "_error",
            "fields": { "status_text": status, },
            "tags": errtags,
            "time": ts,
            })

        got_status = True

    if any(indata):
        n0 = 0

        while n0 < indata.shape[0]-fftsize:
            mags = np.abs(np.fft.rfft(indata[n0:n0+2*fftsize, 0]))
            mags *= mags

            bin_accumulator += mags[:-1]  # drop the nyquist bin
            n_samples += 1
            n0 += fftsize
    else:
        logging.warning("No data received in callback")

        # Don't report empty data if we got a status message
        if not got_status:
            errtags = TAGS.copy()
            errtags["error"] = "got_status"

            frame_queue.append({
                "measurement": args.measurement_name + "_error",
                "fields": { "status_text": "Empty data" },
                "time": ts,
            })


try:
    with sd.InputStream(device=args.device, channels=1, callback=callback,
                        blocksize=args.blocksize, samplerate=samplerate):

        n_zero_loops = 0
        max_zero_loops = 3

        while True:
            time.sleep(args.time)

            splits = [ 100, 500, 1000, 2500, 5000, 10000, ]
            mag_sums = np.zeros_like(splits)
            mag_cnts = np.zeros_like(mag_sums)

            # Send the backlog of error messages if we have one
            if frame_queue:
                logging.debug(f'Submitting {len(frame_queue)} errors')
                client.write_points(frame_queue)
                frame_queue.clear()

            # Record the overall RMS, and number of samples
            results = {
                "rms": np.sqrt(np.sum(bin_accumulator)/len(bin_accumulator)/n_samples),
                "n_samples": n_samples,
            }

            # Compute the aggregated frequency range values
            hz_per_bin = samplerate/fftsize
            f_nyquist = samplerate//2  # integer for formatting
            last_bin1 = 1  # skip DC
            for i, f_cut in enumerate(splits):
                if f_cut > f_nyquist:
                    break

                bin0 = last_bin1
                bin1 = int(min(f_cut//hz_per_bin, fftsize))

                mag_sums[i] += np.sum(bin_accumulator[bin0:bin1])
                mag_cnts[i] = bin1 - bin0

                last_bin1 = bin1

            if not n_samples:
                n_zero_loops += 1
                if n_zero_loops >= max_zero_loops:
                    logging.error(f'Got {n_zero_loops} loops without any samples recorded, bailing')
                    res_body = [{
                        "measurement": args.measurement_name + "_error",
                        "fields": { "status_text": "Too many zero samples",
                                    "n_zero_loops": n_zero_loops, },
                        "time":  datetime.datetime.utcnow().isoformat()
                    }]
                    client.write_points(res_body)

                    parser.exit(f'Exceeded max zero sample loops, exiting to restart sound device')
            else:
                # Generate our per-range results
                last_fcut = 0
                for i, f_cut in enumerate(splits):
                    if f_cut > f_nyquist:
                        break

                    lbl = f"f{last_fcut}-{f_cut}"
                    results[lbl] = np.sqrt(mag_sums[i]/mag_cnts[i]/n_samples)
                    last_fcut = f_cut

            n_samples = 0
            bin_accumulator = np.zeros_like(bin_accumulator)

            # And bundle it all up to send
            ts = datetime.datetime.utcnow().isoformat()
            d = {
                "measurement": args.measurement_name,
                "tags": TAGS,
                "time": ts,
                "fields": results,
            }

            logging.debug(d)

            json_body = [d]
            client.write_points(json_body)
            logging.debug(f'Sent datapoints: {json_body}')

except KeyboardInterrupt:
    logging.error('Got keybord interrupt')
    parser.exit('Interrupted')
except Exception as e:
    logging.error(f'Got exception: {e}')
    parser.exit(type(e).__name__ + ": " + str(e))
