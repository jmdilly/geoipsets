# maxmind.py

import hashlib
import os
import shutil
from collections import Counter
from csv import DictReader
from io import TextIOWrapper
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZipFile

import requests

from . import utils


class MaxMindProvider(utils.AbstractProvider):
    """MaxMind IP range set provider."""

    def __init__(self, firewall: set, address_family: set, checksum: bool, countries: set, output_dir: str,
                 provider_options: dict):
        """'provider_options' is a ConfigParser Section that can be treated as a dictionary.
            Use this mechanism to introduce provider-specific options into the configuration file."""
        super().__init__(firewall, address_family, checksum, countries, output_dir)

        if not (license_key := provider_options.get('license-key')):
            raise RuntimeError("License key cannot be empty")

        self.license_key = license_key
        self.base_url = 'https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country-CSV&license_key='

    def generate(self):
        zip_file = self.download()  # comment out for testing

        if self.checksum:
            self.check_checksum(zip_file)

        with ZipFile(Path(zip_file.name), 'r') as zip_ref:
            # with ZipFile(Path("/tmp/tmp96kyeecw.zip"), 'r') as zip_ref:  # replace line above with this for testing

            zip_dir_prefix = os.path.commonprefix(zip_ref.namelist())
            cc_map = self.build_map(zip_ref, zip_dir_prefix)

            # TODO: run each address-family concurrently?
            if self.ipv4:
                self.build_sets(cc_map, zip_ref, zip_dir_prefix, utils.AddressFamily.IPV4)

            if self.ipv6:
                self.build_sets(cc_map, zip_ref, zip_dir_prefix, utils.AddressFamily.IPV6)

    def build_map(self, zip_ref: ZipFile, dir_prefix: str):
        """
        Build dictionary mapping geoname_ids to ISO country codes
        {6251999: 'CA', 1269750: 'IN'}
        example row: 6251999,en,NA,"North America",CA,Canada,0

        field names:
        geoname_id, locale_code, continent_code, continent_name, country_iso_code, country_name, is_in_european_union
        """
        locations = 'GeoLite2-Country-Locations-en.csv'
        country_code_map = dict()
        with ZipFile(Path(zip_ref.filename), 'r') as zip_file:
            with zip_file.open(dir_prefix + locations, 'r') as csv_file_bytes:
                rows = DictReader(TextIOWrapper(csv_file_bytes))
                for r in rows:
                    if cc := r['country_iso_code']:
                        # configparser forces keys to lower case by default
                        if self.countries == 'all' or cc.lower() in self.countries:
                            country_code_map[r['geoname_id']] = cc

        return country_code_map

    def build_sets(self, country_code_map: dict, zip_ref: ZipFile, dir_prefix: str, addr_fam: utils.AddressFamily):
        """
        Iterates through IP blocks and builds country-specific IP range lists.
        field names:
        network,geoname_id,registered_country_geoname_id,represented_country_geoname_id,is_anonymous_proxy,is_satellite_provider
        """
        suffix = '.' + addr_fam.value
        ipset_dir = self.base_dir / 'maxmind/ipset' / addr_fam.value
        nftset_dir = self.base_dir / 'maxmind/nftset' / addr_fam.value
        if addr_fam == utils.AddressFamily.IPV4:
            ip_blocks = 'GeoLite2-Country-Blocks-IPv4.csv'
            inet_family = 'family inet'
        else:  # AddressFamily.IPV6
            ip_blocks = 'GeoLite2-Country-Blocks-IPv6.csv'
            inet_family = 'family inet6'

        # remove old sets if they exist
        if ipset_dir.is_dir():
            shutil.rmtree(ipset_dir)

        if nftset_dir.is_dir():
            shutil.rmtree(nftset_dir)

        if self.ip_tables:
            ipset_dir.mkdir(parents=True)
        if self.nf_tables:
            nftset_dir.mkdir(parents=True)

        with ZipFile(Path(zip_ref.filename), 'r') as zip_file:
            with zip_file.open(dir_prefix + ip_blocks, 'r') as csv_file_bytes:
                stream = TextIOWrapper(csv_file_bytes)

                # count the number of entries for each country
                cc_counter = Counter(country_code_map.get(r['geoname_id'] or r['registered_country_geoname_id'])
                                     for r in DictReader(stream))

                # return the stream to the start
                stream.seek(0, 0)
                rows = DictReader(stream)
                for r in rows:
                    geo_id = r['geoname_id']
                    if not geo_id:
                        geo_id = r['registered_country_geoname_id']
                    if not geo_id:
                        continue

                    try:
                        cc = country_code_map[geo_id]
                    except KeyError:
                        continue  # skip CC if not listed in the config file

                    net = r['network']
                    set_name = cc + suffix

                    #
                    # iptables/ipsets
                    #
                    if self.ip_tables:
                        ipset_file = ipset_dir / set_name
                        if not ipset_file.is_file():
                            with open(ipset_file, 'a') as f:
                                # round up to the next power of 2
                                maxelem = max(131072,
                                              1 if cc_counter[cc] == 0 else (1 << (cc_counter[cc] - 1).bit_length()))
                                f.write("create {0} hash:net {1} maxelem {2} comment\n".format(set_name,
                                                                                               inet_family,
                                                                                               maxelem))

                        with open(ipset_file, 'a') as f:
                            f.write("add " + set_name + " " + net + " comment " + cc + "\n")

                    #
                    # nftables set
                    #
                    if self.nf_tables:
                        nftset_file = nftset_dir / set_name
                        if not nftset_file.is_file():
                            with open(nftset_file, 'a') as f:
                                f.write("define " + set_name + " = {\n")

                        with open(nftset_file, 'a') as f:
                            f.write(net + ",\n")

                # this feels dirty
                if self.nf_tables:
                    for nf_set_file in nftset_dir.iterdir():
                        if nf_set_file.is_file():  # not strictly needed
                            with open(nf_set_file, 'a') as f:
                                f.write("}\n")

    def download(self):
        # URL: https://download.maxmind.com/app/geoip_download
        # CSV query string: ?edition_id=GeoLite2-Country-CSV&license_key=LICENSE_KEY&suffix=zip

        # The downloaded filename is available in the 'Content-Disposition' HTTP response header.
        # eg. Content-Disposition: attachment; filename=GeoLite2-Country-CSV_20200922.zip
        file_suffix = 'zip'
        zip_url = self.base_url + self.license_key + '&suffix=' + file_suffix

        # download latest ZIP file
        zip_http_response = requests.get(zip_url)
        with NamedTemporaryFile(suffix='.' + file_suffix, delete=False) as zip_file:
            zip_file.write(zip_http_response.content)

        return zip_file

    def download_checksum(self):
        # URL: https://download.maxmind.com/app/geoip_download
        # MD5 query string: ?edition_id=GeoLite2-Country-CSV&license_key=LICENSE_KEY&suffix=zip.md5
        file_suffix = 'zip.md5'
        md5_url = self.base_url + self.license_key + '&suffix=' + file_suffix
        md5_http_response = requests.get(md5_url)
        with NamedTemporaryFile(suffix='.' + file_suffix, delete=False) as md5_file:
            md5_file.write(md5_http_response.content)
            md5_file.seek(0)

            return md5_file.read().decode('utf-8')

    def check_checksum(self, zip_ref):
        expected_md5sum = self.download_checksum()

        # calculate md5 hash
        with open(zip_ref.name, 'rb') as raw_zip_file:
            md5_hash = hashlib.md5()
            # Read and update hash in 8K chunks
            while chunk := raw_zip_file.read(8192):
                md5_hash.update(chunk)

            computed_md5sum = md5_hash.hexdigest()

        # compare downloaded md5 hash with computed version
        if expected_md5sum != computed_md5sum:
            raise RuntimeError("Computed zip file digest '{0}' does not match expected value '{1}'".format(
                computed_md5sum, expected_md5sum
            ))
