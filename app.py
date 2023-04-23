import base64
import datetime
import uuid
from dateutil import rrule, tz
import dateutil
from flask import Flask, request
import psycopg2
import simplejson
import re
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Content, To
import icalendar
import recurring_ical_events
import urllib.request
import openai
openai.api_key = os.getenv("OPENAI_API_KEY")


ptz = tz.gettz("America/Los_Angeles")
utz = tz.gettz("UTC")
app = Flask(__name__)

users = ["okgodoit@gmail.com", "aqeel@cerebralvalley.ai", "roger@pincombe.com"]
email_regex = r'(?:"?([^"]*)"?\s)?(?:<?(.+@[^>]+)>?)'


@app.route('/process-inbound-email', methods=['POST'])
def sendgrid_parser():
    # Consume the entire email
    envelope = simplejson.loads(request.form.get('envelope'))

    # Get some header information
    to_addresses = request.form.get('to') or envelope['to']
    from_address = request.form.get('from') or envelope['from']

    # use regex to parse out the actual email address in the case of a formatted name&email combo
    m = re.search(email_regex, from_address)
    if m:
        from_address = m.group(2)

    cc_addresses = request.form.get('cc')
    is_reply_from_recipient = True
    owner_user = ''
    recipient = ''

    for to_address in to_addresses.split(','):
        to_address = to_address.strip().lower()
        # use regex to parse out the actual email address in the case of a formatted name&email combo
        m = re.search(email_regex, to_address)
        if m:
            to_address = m.group(2)

        if to_address == 'assistant@ask.okgodoit.com':
            0  # this is me so ignore
        elif to_address in users:
            owner_user = to_address
        else:
            recipient = to_address
    if cc_addresses:
        for cc_address in cc_addresses.split(','):
            cc_address = cc_address.strip().lower()
            # use regex to parse out the actual email address in the case of a formatted name&email combo
            m = re.search(email_regex, cc_address)
            if m:
                cc_address = m.group(2)
            if cc_address == 'assistant@ask.okgodoit.com':
                0  # this is me so ignore
            elif cc_address in users:
                owner_user = to_address

    if from_address in users:
        owner_user = from_address
        is_reply_from_recipient = False
    elif recipient == '':
        recipient = from_address
        is_reply_from_recipient = True

    # Now, onto the body
    text = request.form.get('text') or request.form.get('html')

    subject = request.form.get('subject')

    # write parsed info to console
    print('Owner: %s' % owner_user)
    print('Recipient: %s' % recipient)
    print('Is Reply From Target: %s' % is_reply_from_recipient)
    print('Subject: %s' % subject)
    print('Text: %s' % text)

    # now try to find the owner user
    conn = psycopg2.connect(
        host="localhost",
        database="assistant",
        user="assistantuser",
        password=os.environ.get('PSQL_PASSWORD'))
    conn.autocommit = True

    ical_url = None
    isConfirmed = False

    cur = conn.cursor()
    cur.execute("SELECT email_address, thread_history, primaryuserid, isConfirmed, users.email as ownerEmail, users.ical_url as ownerIcal, users.full_name as ownerFullName, users.other_info_for_prompt as otherPromptInfo FROM public.externalusers join users on primaryuserid = users.user_id where externalusers.email_address=%s", (recipient,))
    result = cur.fetchone()
    if result and result[0]:
        thread_history = result[1]
        owner_user_id = result[2]
        isConfirmed = result[3]
        owner_user = result[4]
        ical_url = result[5]
        fullOwnerName = result[6]
        otherOwnerInfo = result[7]
        print('Thread found with owner %s and recipient %s' %
              (owner_user, recipient))
    else:
        print('no current thread found')
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, email, ical_url, full_name, other_info_for_prompt FROM public.users where email=%s;", (owner_user,))
        result = cur.fetchone()
        if result and result[1] == owner_user:
            print('owner user %s found, new thread started with recipient %s' %
                  (owner_user, recipient))
            thread_history = ''
            owner_user_id = result[0]
            ical_url = result[2]
            fullOwnerName = result[3]
            otherOwnerInfo = result[4]

            cur = conn.cursor()
            cur.execute("INSERT INTO public.externalusers (email_address, thread_history, primaryuserid) VALUES (%s, %s, %s) RETURNING *;",
                        (recipient, thread_history, owner_user_id))
            dbinstertresult = cur.fetchone()
            print(dbinstertresult)
        else:
            # no associated main user and no exisiting thread, disreagard for now
            print('No associated owner and no exisiting thread, disreagard for now')
            return 'OK'

    calendar_summary = ''

    thread_history += '\n\nOn %s %s wrote:\n%s\n%s' % (
        datetime.datetime.now().astimezone(ptz).strftime("%A %B %d, %Y at %-I:%M%p"), from_address, subject, text.replace(thread_history, ''))
    cur = conn.cursor()
    cur.execute("UPDATE public.externalusers SET thread_history=%s WHERE email_address=%s AND primaryuserid=%s;",
                (thread_history, recipient, owner_user_id))

    if is_reply_from_recipient:
        prompt = "Given the following email exchange, has a date and time for the meeting between %s and %s been agreed upon and confirmed?  Is yes, reply with json { \"confirmed\": true, \"start_time\": <start date and time in pacific time zone>, \"end_time\": <end date and time in pacific time zone>, \"location\": <the location for the meeting if specified>, \"meeting_title\": <the title or summary of the meeting for context> }.  If no, reply with json { \"confirmed\": false }.  Return only a valid json object and nothing else.\n\n%s" % (
            fullOwnerName, recipient, thread_history)
        completion = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are %s's executive assistant and your primary job is to schedule calendar appointments for them." % (
                    fullOwnerName,)},
                {"role": "user", "content": prompt},
            ]
        )
        confirmedJson = simplejson.loads(completion.choices[0].message.content)
        isConfirmed = confirmedJson.get('confirmed', False)
        if isConfirmed:
            eventStart = dateutil.parser.parse(confirmedJson.get(
                'start_time'), ignoretz=True).replace(tzinfo=ptz).astimezone(utz)
            eventEnd = dateutil.parser.parse(confirmedJson.get(
                'end_time'), ignoretz=True).replace(tzinfo=ptz).astimezone(utz)
            eventLocation = confirmedJson.get('location', None)
            eventTitle = "Meeting between %s and %s" % (
                fullOwnerName, recipient)
            cal = icalendar.Calendar()
            cal.add('prodid', '-//Okgodoit Assistant//okgodoit.com//')
            cal.add('version', '2.0')

            event = icalendar.Event()
            event.add('summary', eventTitle)
            event.add('description', eventTitle)
            istart = icalendar.vDatetime(eventStart)
            event.add('dtstart', istart)
            iend = icalendar.vDatetime(eventEnd)
            event.add('dtend', iend)
            event.add('dtstamp', datetime.datetime.now().astimezone(utz))

            # Add the organizer
            organizer = icalendar.vCalAddress('MAILTO:'+owner_user)

            # Add parameters of the event
            organizer.params['cn'] = icalendar.vText(fullOwnerName)

            if eventLocation:
                event['location'] = icalendar.vText(eventLocation)

            event['uid'] = str(uuid.uuid4())

            attendee = icalendar.vCalAddress('MAILTO:'+recipient)
            attendee.params['cn'] = icalendar.vText(recipient)
            attendee.params['role'] = icalendar.vText('REQ-PARTICIPANT')
            attendee.params['rsvp'] = icalendar.vBoolean(True)
            event.add('attendee', attendee)
            event.add('attendee', organizer)

            organizer.params['role'] = icalendar.vText('REQ-PARTICIPANT')
            organizer.params['rsvp'] = icalendar.vBoolean(True)
            event['organizer'] = organizer

            alarm = icalendar.Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add('trigger', datetime.timedelta(minutes=-15))
            alarm.add('description', 'Reminder for ' + eventTitle)
            event.add_component(alarm)
            event.add('sequence', 0)
            event.add('status', 'CONFIRMED')

            # Add the event to the calendar
            cal.add_component(event)

            ical = cal.to_ical().decode('utf-8')

            message = Mail(
                from_email='assistant@ask.okgodoit.com',
                to_emails=[To(owner_user), To(recipient)],
                subject='Calendar Invitation: ' + eventTitle)
            contentObj = Content("text/calendar; method=REQUEST", ical)
            print(contentObj)
            message.content = contentObj
            contentObj = Content("text/plain", "Your meeting with %s and %s is confirmed for %s" %
                                 (owner_user, recipient, eventStart.strftime("%A, %B %d, %Y at %I:%M %p")))
            message.content = contentObj

            try:
                sg = SendGridAPIClient(os.environ.get('SENDGRID_API_KEY'))
                response = sg.send(message)
                print(response.status_code)
                print(response.body)
                print(response.headers)
                
                cur = conn.cursor()
                cur.execute("UPDATE public.externalusers SET isconfirmed=%s WHERE email_address=%s AND primaryuserid=%s;",
                            (True, recipient, owner_user_id))
            except Exception as e:
                print(e.message)
            return 'OK'

    if ical_url:
        # get all calendar events for the next month
        ical_string = urllib.request.urlopen(ical_url).read()
        calendar = recurring_ical_events.of(
            icalendar.Calendar.from_ical(ical_string))

        # iterate every date between today and 20 days from now
        for dayToCheck in rrule.rrule(rrule.DAILY, dtstart=datetime.datetime.now().astimezone(ptz), until=datetime.datetime.now().astimezone(ptz) + datetime.timedelta(days=20)):
            # print("checking %s" % dayToCheck.strftime("%A %B %d, %Y"))
            events = calendar.at(
                (dayToCheck.year, dayToCheck.month, dayToCheck.day))
            if len(events) == 0:
                calendar_summary += '%s: Available all day\n' % dayToCheck.strftime(
                    "%A %B %d, %Y")
                continue
            events.sort(key=lambda x: x.decoded("DTSTART").astimezone(ptz))
            lastEventEndTime = dayToCheck.replace(
                hour=7, minute=0, second=0, microsecond=0)
            for component in events:
                if component.name == "VEVENT":
                    if type(component.decoded("DTSTART")) is datetime.datetime:
                        freeTimeLength = component.decoded(
                            "DTSTART").astimezone(ptz) - lastEventEndTime
                        if freeTimeLength >= datetime.timedelta(minutes=30):
                            calendar_summary += '%s: Available from %s to %s\n' % (
                                dayToCheck.strftime("%A %B %d, %Y"), lastEventEndTime.strftime("%-I:%M%p"), component.decoded("DTSTART").astimezone(ptz).strftime("%-I:%M%p"))
                        lastEventEndTime = component.decoded(
                            "DTEND").astimezone(ptz)
                        calendar_summary += '%s: Busy from %s to %s\n' % (
                            dayToCheck.strftime("%A %B %d, %Y"), component.decoded("DTSTART").astimezone(ptz).strftime("%-I:%M%p") if component.decoded("DTSTART").astimezone(ptz) > dayToCheck.replace(hour=7, minute=0, second=0, microsecond=0) else "morning", component.decoded("DTEND").astimezone(ptz).strftime("%-I:%M%p") if component.decoded("DTEND").astimezone(ptz) < dayToCheck.replace(hour=23, minute=59, second=59, microsecond=0) else "midnight")
            if lastEventEndTime < dayToCheck.replace(hour=19, minute=0, second=0, microsecond=0):
                freeTimeLength = dayToCheck.replace(
                    hour=19, minute=0, second=0, microsecond=0) - lastEventEndTime
                if freeTimeLength >= datetime.timedelta(minutes=30):
                    calendar_summary += '%s: Available from %s to %s\n' % (
                        dayToCheck.strftime("%A %B %d, %Y"), lastEventEndTime.strftime("%-I:%M%p"), dayToCheck.replace(hour=19, minute=0, second=0, microsecond=0).strftime("%-I:%M%p"))
        if (calendar_summary == ''):
            calendar_summary = 'The calendar is completely empty and available'

        # if is_reply_from_recipient and calendar.at()

    prompt = "You are %s's executive assistant and your primary job is to schedule calendar appointments for them.\n%s\nToday is %s. It is currently %s. All timezones are %s. Generally meeting and events should be scheduled during normal business hours, unless otherwise specified.\n\nHere is a summary of %s's availability:\n%s\nAny time within those ranges is available to be scheduled, although there is a slight preference for leaving a 30 minute buffer between events if possible. Do not share this full listing with the user directly.\n\nHere's the email thread so far:\n%s\nPlease write a %s to %s as %s's assistant to help schedule this meeting within the next few days, or weeks, or as requested.  Give a general sense of availability and suggest a few days, times, or ranges that are available for this meeting, or allow the other person to suggest a date and time that is best for them. Please take into account any specific requests from %s or %s in the email thread, although the provided schedule availability takes precedence. Be concise, professional, and polite. Do not express human emotion or sentiment, you are just a tool. Sign the email \"OkGoDoIt assistant on behalf of %s\"" % (
        fullOwnerName,
        otherOwnerInfo,
        datetime.datetime.now().astimezone(ptz).strftime("%A %A, %B %d, %Y"),
        datetime.datetime.now().astimezone(ptz).strftime("%-I:%M%p"),
        datetime.datetime.now().astimezone(ptz).tzname(),
        fullOwnerName,
        calendar_summary,
        thread_history,
        "reply" if is_reply_from_recipient else "follow up email",
        recipient,
        fullOwnerName,
        fullOwnerName,
        recipient,
        fullOwnerName
    )

    print("prompt to chatgpt: " + prompt)

    completion = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are %s's executive assistant and your primary job is to schedule calendar appointments for them." % (
                fullOwnerName,)},
            {"role": "user", "content": prompt},
        ]
    )

    reply = completion.choices[0].message.content

    print("reply from chatgpt: " + reply)

    """  if (owner_user == ''):
        reply_content = 'This is a test email reply from the okgodoit scheduling assistant.\n\nOn %s %s wrote:\n%s' % (
            datetime.now().strftime("%B %d, %Y at %-I:%M%p"), from_address, text)
    else:
        reply_content = 'This is a test email reply from the okgodoit scheduling assistant on behalf of %s.\n\nOn %s %s wrote:\n%s' % (
            owner_user, datetime.now().strftime("%B %d, %Y at %-I:%M%p"), from_address, text) """

    message = Mail(
        from_email='assistant@ask.okgodoit.com',
        to_emails=recipient,  # should be recipient once testing is good
        subject='Re: ' + subject,
        plain_text_content=reply)
    try:
        sg = SendGridAPIClient(os.environ.get('SENDGRID_API_KEY'))
        response = sg.send(message)
        print(response.status_code)
        print(response.body)
        print(response.headers)
    except Exception as e:
        print(e.message)

    return "OK"


if __name__ == '__main__':
    app.run(debug=True)
