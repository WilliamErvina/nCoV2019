import logging
import os
from datetime import datetime
from typing import List
import configparser

import pandas as pd

from geocoding import csv_geocoder
from spreadsheet import GoogleSheet

from functions import (duplicate_rows_per_column, fix_na, fix_sex,
                       generate_error_tables, trim_df, values2dataframe)


class SheetProcessor:

    def __init__(self, sheets: List[GoogleSheet], geocoder: csv_geocoder.CSVGeocoder, config: configparser.ConfigParser):
        self.for_github = []
        self.sheets = sheets
        self.geocoder = geocoder
        self.config = config

    def process(self):
        """Does all the heavy handling of spreadsheets, writing output to CSV files."""
        for s in self.sheets:
            logging.info("Processing sheet %s", s.name)

            ### Clean Private Sheet Entries. ###
            # note : private sheet gets updated on the fly and redownloaded to ensure continuity between fixes (granted its slower).
            
            range_ = f'{s.name}!A:AG'
            data = values2dataframe(s.read_values(range_))

            # Expand aggregated cases into one row each.
            logging.info("Rows before expansion: %d", len(data))
            if len(data) > 150000:
                logging.warning("Sheet %s has more than 150K rows, it should be split soon", s.name)
            data.aggregated_num_cases = pd.to_numeric(data.aggregated_num_cases, errors='coerce')
            data = duplicate_rows_per_column(data, "aggregated_num_cases")
            logging.info("Rows after expansion: %d", len(data))

            # Generate IDs for each row sequentially following the sheet_id-inc_int pattern.
            data['ID'] = s.base_id + "-" + pd.Series(range(1, len(data)+1)).astype(str)

            # Remove whitespace.
            data = trim_df(data)

            # Fix columns that can be fixed easily.
            data.sex = fix_sex(data.sex)

            # fix N/A => NA
            for col in data.select_dtypes("string"):
                data[col] = fix_na(data[col])

            # Regex fixes
            fixable, non_fixable = generate_error_tables(data)
            if len(fixable) > 0:
                logging.info('fixing %d regexps', len(fixable))
                s.fix_cells(fixable)
                data = values2dataframe(s.read_values(range_))
            
            # ~ negates, here clean = data with IDs not in non_fixable IDs.
            clean = data[~data.ID.isin(non_fixable.ID)]
            clean = clean.drop('row', axis=1)
            clean.sort_values(by='ID')
            s.data = clean
            non_fixable = non_fixable.sort_values(by='ID')

            # Save error_reports
            # These are separated by Sheet.
            logging.info('Saving error reports')
            directory   = self.config['FILES']['ERRORS']
            file_name   = f'{s.name}.error-report.csv'
            error_file  = os.path.join(directory, file_name)
            non_fixable.to_csv(error_file, index=False, header=True, encoding="utf-8")
            self.for_github.append(error_file)
            
        # Combine data from all sheets into a single datafile
        all_data = []
        for s in self.sheets:
            logging.info("sheet %s had %d rows", s.name, len(s.data))
            all_data.append(s.data)
        
        all_data = pd.concat(all_data, ignore_index=True)
        all_data = all_data.sort_values(by='ID')
        logging.info("all_data has %d rows", len(all_data))

        # Fill geo columns.
        geocode_matched = 0
        for i, row in all_data.iterrows():
            geocode = self.geocoder.geocode(row.city, row.province, row.country)
            if not geocode:
                continue
            geocode_matched += 1
            all_data.at[i, 'latitude'] = geocode.lat
            all_data.at[i, 'longitude'] = geocode.lng
            all_data.at[i, 'geo_resolution'] = geocode.geo_resolution
            all_data.at[i, 'location'] = geocode.location
            all_data.at[i, 'admin3'] = geocode.admin3
            all_data.at[i, 'admin2'] = geocode.admin2
            all_data.at[i, 'admin1'] = geocode.admin1
            all_data.at[i, 'admin_id'] = geocode.admin_id
            all_data.at[i, 'country_new'] = geocode.country_new
        logging.info("Geocode matched %d/%d", geocode_matched, len(all_data))
        logging.info("Top 10 geocode misses: %s", self.geocoder.misses.most_common(10))
        with open("geocode_misses.csv", "w") as f:
            self.geocoder.write_misses_to_csv(f)
            logging.info("Wrote all geocode misses to geocode_misses.csv")
        # Reorganize csv columns so that they are in the same order as when we
        # used to have those geolocation within the spreadsheet.
        # This is to avoid breaking latestdata.csv consumers.
        all_data = all_data[["ID","age","sex","city","province","country","latitude","longitude","geo_resolution","date_onset_symptoms","date_admission_hospital","date_confirmation","symptoms","lives_in_Wuhan","travel_history_dates","travel_history_location","reported_market_exposure","additional_information","chronic_disease_binary","chronic_disease","source","sequence_available","outcome","date_death_or_discharge","notes_for_discussion","location","admin3","admin2","admin1","country_new","admin_id","data_moderator_initials","travel_history_binary"]]

        # save
        logging.info("Saving files to disk")
        dt = datetime.now().strftime('%Y-%m-%dT%H%M%S')
        file_name   = self.config['FILES']['DATA'].replace('TIMESTAMP', dt)
        latest_name = os.path.join(self.config['FILES']['LATEST'], 'latestdata.csv')
        all_data.to_csv(file_name, index=False, encoding="utf-8")
        all_data.to_csv(latest_name, index=False, encoding="utf-8")
        logging.info("Wrote %s, %s", file_name, latest_name)
        self.for_github.extend([file_name, latest_name])

    def push_to_github(self):
        """Pushes csv files created by Process to Github."""
        logging.info("Pushing to github")
        # Create script for uploading to github
        script  = 'set -e\n'
        script += 'cd {}\n'.format(self.config['GIT']['REPO'])
        script += 'git pull origin master\n'
        
        for g in self.for_github:
            script += f'git add {g}\n'
        script += 'git commit -m "data update"\n'
        script += 'git push origin master\n'
        script += f'cd {os.getcwd()}\n'
        print(script)
        os.system(script)
