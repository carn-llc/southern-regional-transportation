# Solution Design

# Inputs

- Empty Excel sheet  
- List of available drivers with numbers associated

#  Computation Tasks

-   
- Read the excel sheet

# Outputs

- Full Excel sheet with 

# How we will operate

I would like to make sure we have a concrete plan before we write any code. Please lay out all of the details regarding functions, data structures, algorithms, and justification. We should do this function by function. Planning one function and writing it before moving to the next one

# Goal

- I am looking to create a python script that will automate a manual process I have as a high school transportation manager. Each week I have to manually go through PDF spreadsheets and assign trips based on a number of factors including bus driver priority, trips available, and date/times of trips in the most fair way possible. The automation must match the arbitrary set of rules that we currently follow manually. The rules and details of how this script will be developed are laid out below, please optimize the algorithm and data structures used to best fit our set of rules for assigning trips.

# Important Details

- Functions  
  - pre-pull-data-extraction() – Scrape the data from the trip data sheet and store it in the most appropriate data structure such that all data from a single trip can be referenced together. The following should be stored for each trip. Ensure that the trip objects are in an ordered list in chronological order (Starting Monday Morning and Ending Friday Night). This function will take a pdf with the format of the trip-pull-details.png  
    - The data that should be scraped is the following:  
      - Each pull has the following details  
        - Pull type (weekday, saturday, sunday)  
        - Starting Seniority Number  
      - Each Trip Has Following Details  
        - Date  
        - Day  
        - Destination  
        - Group  
        - Departure time  (24 hour clock)  
        - Return time (24 hour clock)  
        - Number of buses  
      - Each Driver has following details  
        - First Name  
        - Last Name  
        - Seniority Number  
        - First Choice For Trip in this Pull (only one allowed)  
  - run-pull() – One function agnostic of the type of pull. Takes the following information  
    - Take the following information  
      - Starting seniority \#  
      - Ordered List of trips  
      - \# of buses  
    - Steps  
      - Start at driver \# x  
        - Is First Choice=True present across any trips?  
          - Yes → Is the trip bus counter greater than 0?  
            - Yes → Assign trip, reduce bus count by 1 for that trip  
            - No → Is the driver listed on any other trips with a bus count greater than 0?  
              - Yes → Assign the first trip that the driver is listed for that does not overlap (date and time) with other trips the driver has already been assigned  
              - No → Move to driver x+1  
          - No → Is the driver listed on any other trips with a bus count greater than 0?  
            - Yes → Assign the first trip that the driver is listed for that does not overlap (date and time) with other trips the driver has already been assigned  
            - No → Move to next driver  
      - Once all drivers have been passed through, go back to driver x and start over the above process until all trips have been assigned, or all drivers have been exhausted (no remaining drivers without overlapping trips for remaining trips)  
  - post-pull-validation() – Execute validation to confirm potential edge cases havent errored  
    - If a driver has multiple trips on a single day, print them out for manual confirmation there is no overlap  
    - Print out any unassigned trips to manually confirm that no one selected preference for these. If so, these will go to substitutes  